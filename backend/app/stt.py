"""STT provider layer for Phase 5 ambient mode.

Side-STT architecture (product_phases.md, 25 Aug 2026): ambient mic audio goes to a
dedicated STT+diarization provider; Gemini Live connects ONLY on invocation.

Providers are selected by env config so vendors can swap without code changes:
    SIRIOUS_STT_PROVIDER = google | deepgram | assembly   (default: google)
    SIRIOUS_STT_LANGS    = comma list, PINNED (default "en-IN,hi-IN")
        NEVER use auto-detect with a wide language set — Sirious historically
        misfired into Tamil/Telugu etc. Pin the language list explicitly.
    SIRIOUS_STT_LOCATION = Google v2 region (default "us" — Chirp models are
        regional, not global)
    SIRIOUS_STT_MIN_SPEAKERS / SIRIOUS_STT_MAX_SPEAKERS (defaults 2)

Data-logging tier (Google free 60 min/mo vs no-logging rate) is a PROJECT-level
Cloud Console setting, not a per-request flag — owned by Paresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional


def _langs_from_env() -> list[str]:
    raw = os.environ.get("SIRIOUS_STT_LANGS", "en-IN,hi-IN")
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class DiarizedWord:
    word: str
    start: float
    end: float
    speaker_tag: int


@dataclass
class DiarizedUtterance:
    speaker_tag: int
    text: str
    start: float
    end: float
    words: list[DiarizedWord] = field(default_factory=list)


class SttProvider(ABC):
    """One live streaming session: open -> feed PCM -> utterance events -> close."""

    name: str = "base"

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def feed(self, pcm16_mono: bytes, sample_rate: int = 16000) -> None: ...

    @abstractmethod
    async def stop(self) -> list[DiarizedUtterance]: ...


class DeepgramAmbient:
    """Deepgram streaming STT with diarization — the C1 ambient hot path.

    Verified against the live API 25 Aug (backend/deepgram_smoke.py):
    - nova-3 + language=multi gives en<->hi code-switching with per-word
      speaker tags;
    - live payloads are FLAT: msg["channel"]["alternatives"][0] (NOT the
      REST nesting results.channels[0]);
    - raw PCM only (linear16/16000/1 declared in the query string) — a WAV
      container connects but silently yields nothing;
    - Deepgram drops idle connections (1011, ~10-15 s of dead air) unless
      KeepAlive JSON messages flow — this class sends them on a timer.
    """

    name = "deepgram"

    def __init__(
        self,
        on_segment: Callable[[DiarizedUtterance], None],
        *,
        model: str | None = None,
        language: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.on_segment = on_segment
        self.model = model or os.environ.get("SIRIOUS_STT_MODEL", "nova-3")
        self.language = language or os.environ.get("SIRIOUS_STT_DG_LANG", "multi")
        self.api_key = api_key or os.environ.get("DEEPGRAM_KEY", "")
        self._ws: Any = None
        self._recv_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._closed = False
        self.segments_seen = 0

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.api_key:
            raise RuntimeError("DEEPGRAM_KEY not configured")
        import websockets

        qs = "&".join(f"{k}={v}" for k, v in self._params().items())
        url = f"wss://api.deepgram.com/v1/listen?{qs}"
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {self.api_key}"},
            max_size=10 * 1024 * 1024,
        )
        self._closed = False
        self._recv_task = asyncio.create_task(self._recv_loop(), name="dg-recv")
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="dg-keepalive"
        )

    async def feed(self, pcm16_mono: bytes, sample_rate: int = 16000) -> None:
        if self._ws is not None and not self._closed:
            await self._ws.send(pcm16_mono)

    async def close(self) -> None:
        self._closed = True
        for task in (self._keepalive_task, self._recv_task):
            if task is not None:
                task.cancel()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None

    # ── internals ────────────────────────────────────────────────────────

    def _params(self) -> dict[str, str]:
        """Streaming query params. Extracted from start() so tests can assert
        the wire contract (keyword boost, finals-only, raw-PCM) without
        opening a network socket."""
        params = {
            "model": self.model,
            "language": self.language,
            "diarize": "true",
            "punctuate": "true",
            "smart_format": "true",
            "encoding": "linear16",
            "sample_rate": "16000",
            "channels": "1",
            # Finals only: interims produced duplicate/partial turns in the
            # first on-device test (26 Aug). Endpointing still yields one
            # final per utterance, which is exactly our turn granularity.
            "interim_results": "false",
        }
        # C2 invocation: pin the product name in the transcript so the
        # on-device spotter sees "Sirious" instead of a phonetic lookalike
        # ("serious" is a false-positive minefield in office Hinglish).
        keyword = os.environ.get("SIRIOUS_STT_KEYWORD", "Sirious").strip()
        if keyword:
            params["keywords"] = keyword
        return params

    async def _keepalive_loop(self) -> None:
        """Deepgram 1011-closes silent connections; KeepAlive JSON prevents it
        without affecting transcription (ambient has real dead-air minutes)."""
        import json as _json

        try:
            while not self._closed:
                await asyncio.sleep(5)
                if self._ws is not None:
                    with contextlib.suppress(Exception):
                        await self._ws.send(_json.dumps({"type": "KeepAlive"}))
        except asyncio.CancelledError:
            raise

    async def _recv_loop(self) -> None:
        import json as _json

        try:
            while not self._closed:
                msg = await self._ws.recv()
                data = _json.loads(msg)
                if data.get("type") != "Results":
                    continue
                seg = parse_deepgram_results(data)
                if seg is not None and seg.text.strip():
                    self.segments_seen += 1
                    self.on_segment(seg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — provider errors must not kill the WS handler
            logging.getLogger("sirious.stt").exception("deepgram recv loop died")


def parse_deepgram_results(data: dict[str, Any]) -> DiarizedUtterance | None:
    """Flatten one Deepgram live Results message into a segment.

    Speaker = majority tag across the message's words (interims can carry
    partial words; finals dominate in practice). Times are stream-relative
    seconds from Deepgram.
    """
    alt = data.get("channel", {}).get("alternatives", [{}])[0]
    words = alt.get("words") or []
    if not words:
        return None
    text = (alt.get("transcript") or "").strip()
    if not text:
        return None
    tags = [int(w.get("speaker", 0)) for w in words]
    speaker = max(set(tags), key=tags.count)
    return DiarizedUtterance(
        speaker_tag=speaker,
        text=text,
        start=float(words[0].get("start", 0.0)),
        end=float(words[-1].get("end", 0.0)),
    )


def group_ambient_segments(
    segments: Iterable[DiarizedUtterance],
    *,
    gap_s: float = 2.0,
) -> list[DiarizedUtterance]:
    """Merge consecutive same-speaker segments (gap <= gap_s) into turns."""
    out: list[DiarizedUtterance] = []
    cur: DiarizedUtterance | None = None
    for seg in segments:
        if (
            cur is not None
            and seg.speaker_tag == cur.speaker_tag
            and seg.start - cur.end <= gap_s
        ):
            cur.text = f"{cur.text} {seg.text}".strip()
            cur.end = seg.end
        else:
            if cur is not None:
                out.append(cur)
            cur = DiarizedUtterance(
                speaker_tag=seg.speaker_tag,
                text=seg.text,
                start=seg.start,
                end=seg.end,
            )
    if cur is not None:
        out.append(cur)
    return out


class GoogleSttProvider(SttProvider):
    """Google Cloud Speech-to-Text v2, streaming, diarization on.

    Launch provider (25 Aug 2026): only one of the three candidates with
    STREAMING Marathi; already in our GCP; strong Hinglish. Streaming wiring
    lands with the ambient WS mode (C1) — the batch helper below is the
    credential/API/diarization verification path used by stt_smoke.py.
    """

    name = "google"

    def __init__(self, langs: Optional[list[str]] = None):
        self.langs = langs or _langs_from_env()
        self.location = os.environ.get("SIRIOUS_STT_LOCATION", "us")
        self.min_speakers = int(os.environ.get("SIRIOUS_STT_MIN_SPEAKERS", "2"))
        self.max_speakers = int(os.environ.get("SIRIOUS_STT_MAX_SPEAKERS", "2"))

    async def start(self) -> None:
        raise NotImplementedError("streaming wiring lands with C1 ambient WS mode")

    async def feed(self, pcm16_mono: bytes, sample_rate: int = 16000) -> None:
        raise NotImplementedError("streaming wiring lands with C1 ambient WS mode")

    async def stop(self) -> list[DiarizedUtterance]:
        raise NotImplementedError("streaming wiring lands with C1 ambient WS mode")


def get_provider() -> SttProvider:
    kind = os.environ.get("SIRIOUS_STT_PROVIDER", "google").lower()
    if kind == "google":
        return GoogleSttProvider()
    raise ValueError(
        f"Unknown SIRIOUS_STT_PROVIDER '{kind}'. google ships first; "
        "deepgram/assembly adapters arrive with their keys."
    )


# --------------------------------------------------------------------------
# Batch helper (verification + offline checks; NOT the hot ambient path)
# --------------------------------------------------------------------------

def transcribe_file_with_diarization(
    wav_path: str,
    langs: Optional[list[str]] = None,
    model_candidates: Optional[list[tuple[str, str]]] = None,
    project_id: str = "sirious-2026",
) -> tuple[str, list[DiarizedUtterance]]:
    """Batch-transcribe a 16k mono WAV with speaker diarization.

    model_candidates: ordered (model, location) pairs — chirp_3 lives in
    `global` (verified by API 400 on 25 Aug), regional models in `us` etc.
    Returns (model_used, utterances). Raises the last error if all fail.
    """
    from google.cloud import speech_v2 as speech

    langs = langs or _langs_from_env()
    candidates = model_candidates or [("chirp_3", "global"), ("chirp_2", "us"), ("latest_long", "us")]

    with open(wav_path, "rb") as f:
        content = f.read()

    last_err: Exception | None = None
    for model, location in candidates:
        endpoint = (
            "speech.googleapis.com"
            if location == "global"
            else f"{location}-speech.googleapis.com"
        )
        client = speech.SpeechClient(
            client_options={"api_endpoint": endpoint}
        )
        recognizer = f"projects/{project_id}/locations/{location}/recognizers/_"
        try:
            cfg = speech.RecognitionConfig(
                language_codes=langs,
                model=model,
                features=speech.RecognitionFeatures(
                    # v2 (client 2.40): field is diarization_config; presence
                    # of the config turns diarization ON.
                    diarization_config=speech.SpeakerDiarizationConfig(
                        min_speaker_count=_min_speakers(),
                        max_speaker_count=_max_speakers(),
                    ),
                    enable_automatic_punctuation=True,
                ),
            )
            req = speech.RecognizeRequest(
                recognizer=recognizer, config=cfg, content=content
            )
            resp = client.recognize(request=req)
            return model, _parse_v2_response(resp)
        except Exception as e:  # try next model
            last_err = e
    raise RuntimeError(f"All STT models failed: {last_err}")


def self_location() -> str:
    return os.environ.get("SIRIOUS_STT_LOCATION", "us")


def _min_speakers() -> int:
    return int(os.environ.get("SIRIOUS_STT_MIN_SPEAKERS", "2"))


def _max_speakers() -> int:
    return int(os.environ.get("SIRIOUS_STT_MAX_SPEAKERS", "2"))


def _parse_v2_response(resp) -> list[DiarizedUtterance]:
    """Flatten v2 result into utterances grouped by consecutive speaker tags."""
    out: list[DiarizedUtterance] = []
    if not resp.results:
        return out
    alt = resp.results[-1].alternatives[0] if resp.results[-1].alternatives else None
    if alt is None:
        return out
    cur: Optional[DiarizedUtterance] = None
    for w in alt.words:
        tag = w.speaker_label if hasattr(w, "speaker_label") else getattr(w, "speaker_tag", 0)
        start = float(w.start_time.total_seconds() if hasattr(w.start_time, "total_seconds") else w.start_time)
        end = float(w.end_time.total_seconds() if hasattr(w.end_time, "total_seconds") else w.end_time)
        if cur is None or tag != cur.speaker_tag:
            cur = DiarizedUtterance(speaker_tag=int(tag), text=w.word, start=start, end=end)
            out.append(cur)
        else:
            cur.text += f" {w.word}"
            cur.end = end
        cur.words.append(DiarizedWord(word=w.word, start=start, end=end, speaker_tag=int(tag)))
    return out
