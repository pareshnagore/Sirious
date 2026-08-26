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

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional


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
    model_candidates: Optional[list[str]] = None,
    project_id: str = "sirious-2026",
) -> tuple[str, list[DiarizedUtterance]]:
    """Batch-transcribe a 16k mono WAV with speaker diarization.

    Tries model_candidates in order (Chirp 3 first, latest_long fallback) and
    returns (model_used, utterances). Raises the last error if all fail.
    """
    from google.cloud import speech_v2 as speech

    langs = langs or _langs_from_env()
    model_candidates = model_candidates or ["chirp_3", "latest_long"]

    with open(wav_path, "rb") as f:
        content = f.read()

    client = speech.SpeechClient()
    recognizer = f"projects/{project_id}/locations/{self_location()}/recognizers/_"
    last_err: Exception | None = None

    for model in model_candidates:
        try:
            cfg = speech.RecognitionConfig(
                language_codes=langs,
                model=model,
                features=speech.SpeakerDiarizationConfig(
                    enable_speaker_diarization=True,
                    min_speaker_count=_min_speakers(),
                    max_speaker_count=_max_speakers(),
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
