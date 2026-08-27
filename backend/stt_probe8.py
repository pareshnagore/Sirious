"""Probe 8: gemini-3.5-transcribe — the OPEN diarization question (26 Aug 2026).

Streams the 2-speaker Hinglish fixture over the LIVE API (`gemini-3.5-transcribe-live`)
and checks the decision checklist from product_phases.md Phase 5:
  1. speaker diarization in the live stream  = make-or-break for ambient
  2. Hinglish quality (en-IN/hi-IN code-switch) vs nova-3 multi baseline
  3. per-utterance finals latency vs our finals-only Deepgram pipeline
  4. cost (reported from pricing page, not measured here)

Also runs the BATCH model (`gemini-3.5-transcribe`, interactions API) on the SAME
clip as a diarization control — docs claim spk_1/spk_2 segments there.

Run:  set -a && source .env && set +a && python stt_probe8.py
Env:  GEMINI_API_KEY (never printed).
"""

import asyncio
import os
import sys
import time
import wave

from google import genai
from google.genai import types

WAV = os.path.join(os.environ.get("LOCALAPPDATA", "/tmp"), "Temp", "sirious_hinglish.wav")
if not os.path.exists(WAV):
    WAV = "/tmp/sirious_hinglish.wav"
CHUNK = 3200  # 100 ms at 16 kHz 16-bit mono


def load_pcm() -> tuple[bytes, float]:
    with wave.open(WAV, "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 16000, (
            f"unexpected wav: {w.getnchannels()}ch {w.getsampwidth()}b {w.getframerate()}hz"
        )
        raw = w.readframes(w.getnframes())
    return raw, w.getnframes() / w.getframerate()


async def run_live(client, label: str, cfg: types.LiveConnectConfig) -> dict:
    """One live-session run over the fixture. Returns a summary dict."""
    raw, dur = load_pcm()
    print(f"\n===== LIVE RUN: {label} (clip {dur:.1f}s) =====")
    t_start = time.perf_counter()
    msgs = []
    try:
        async with client.aio.live.connect(
            model="gemini-3.5-transcribe-live", config=cfg
        ) as session:
            print("connected OK")
            # stream the clip in 100 ms chunks, tracking wall time of each chunk
            chunk_times = []
            for off in range(0, len(raw), CHUNK):
                chunk = raw[off : off + CHUNK]
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                )
                chunk_times.append(time.perf_counter())
            # ~3 s of trailing silence so VAD closes the final turn
            for _ in range(30):
                await session.send_realtime_input(
                    audio=types.Blob(data=b"\x00" * CHUNK, mime_type="audio/pcm;rate=16000")
                )
            stream_done = time.perf_counter()
            t_audio_end = chunk_times[-1] if chunk_times else stream_done

            async def collect():
                async for msg in session.receive():
                    msgs.append(msg)

            try:
                async with asyncio.timeout(30):
                    await collect()
            except TimeoutError:
                pass

            # interpret
            texts = []          # final input transcripts
            interims = []       # interim input transcripts
            turn_completes = 0
            first_final_arrival = None
            last_final_arrival = None
            setup = False
            for m in msgs:
                if m.setup_complete:
                    setup = True
                sc = m.server_content
                if sc is None:
                    continue
                if sc.input_transcription and sc.input_transcription.text:
                    arr = time.perf_counter()
                    if first_final_arrival is None:
                        first_final_arrival = arr
                    last_final_arrival = arr
                    d = arr - t_audio_end
                    texts.append((d, sc.input_transcription.text))
                if sc.interim_input_transcription and sc.interim_input_transcription.text:
                    interims.append(sc.interim_input_transcription.text)
                if sc.turn_complete:
                    turn_completes += 1

            summary = {
                "label": label,
                "connected": True,
                "setup": setup,
                "n_msgs": len(msgs),
                "n_interims": len(interims),
                "n_finals": len(texts),
                "n_turn_complete": turn_completes,
                "final_delays_s": [round(d, 2) for d, _ in texts],
                "finals": [t for _, t in texts],
                "interims_sample": interims[:6],
                "first_final_delay_s": round(first_final_arrival - t_audio_end, 2) if first_final_arrival else None,
                "last_final_delay_s": round(last_final_arrival - t_audio_end, 2) if last_final_arrival else None,
            }
            return summary
    except Exception as e:  # noqa: BLE001 - probe reports any failure
        print(f"  ERROR: {e!r}")
        return {"label": label, "connected": False, "error": str(e)[:300]}


def run_batch(client) -> dict:
    """Batch control: gemini-3.5-transcribe (interactions API) with diarization."""
    print("\n===== BATCH CONTROL: gemini-3.5-transcribe (interactions API) =====")
    try:
        f = client.files.upload(file=WAV)
        print(f"uploaded: {f.uri}")
    except Exception as e:  # noqa: BLE001
        print(f"  upload failed: {e!r}")
        return {"ok": False, "error": f"upload: {str(e)[:200]}"}

    try:
        ia = client.interactions.create(
            model="gemini-3.5-transcribe",
            input=[
                {"type": "audio", "uri": f.uri, "mime_type": f.mime_type}
            ],
            generation_config={
                "transcription_config": {
                    "mode": {
                        "type": "verbatim",
                        "diarization_mode": "speaker",
                        "timestamp_granularities": ["word"],
                    }
                }
            },
        )
        out = getattr(ia, "output_text", None) or ""
        print(f"output_text ({len(out)} chars):\n{out[:1500]}")
        # hunt for spk labels anywhere in the interaction content
        blob = repr(ia)[:6000]
        has_spk = ("spk_1" in blob) or ("spk_2" in blob)
        print(f"speaker labels in interaction: {has_spk}")
        if has_spk:
            # show a compact view of a couple of word annotations
            import re

            for m in re.finditer(r"spk_\d", blob):
                i = m.start()
                print(f"  ...{blob[max(0,i-90):i+40]!r}")
                if i > 3000:
                    break
        return {"ok": True, "has_spk": has_spk, "out_len": len(out)}
    except Exception as e:  # noqa: BLE001
        print(f"  interactions.create failed: {e!r}")
        return {"ok": False, "error": f"interactions: {str(e)[:300]}"}


def main() -> int:
    for var in ("GEMINI_API_KEY",):
        if not os.environ.get(var):
            print(f"{var} not set — source backend/.env first")
            return 1
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    results = {}

    # ---- batch control first (sync) ----
    results["batch"] = run_batch(client)

    # ---- live runs (async) ----
    async def live_all():
        r = {}
        r["A_auto_diar"] = await run_live(
            client,
            "A: auto language, diarization=True",
            types.LiveConnectConfig(
                response_modalities=["TEXT"],
                input_audio_transcription=types.AudioTranscriptionConfig(diarization=True),
            ),
        )
        r["B_pinned_diar"] = await run_live(
            client,
            "B: pinned en-IN+hi-IN, diarization=True",
            types.LiveConnectConfig(
                response_modalities=["TEXT"],
                input_audio_transcription=types.AudioTranscriptionConfig(
                    language_codes=["en-IN", "hi-IN"], diarization=True
                ),
            ),
        )
        r["C_auto_nodiar"] = await run_live(
            client,
            "C: auto language, diarization=False (control)",
            types.LiveConnectConfig(
                response_modalities=["TEXT"],
                input_audio_transcription=types.AudioTranscriptionConfig(diarization=False),
            ),
        )
        return r

    results["live"] = asyncio.run(live_all())

    # ---- verdict ----
    print("\n" + "=" * 60)
    print("VERDICT")
    print("=" * 60)
    b = results["batch"]
    print(f"[batch]  ok={b.get('ok')} speaker_labels={b.get('has_spk')} err={b.get('error','')[:120]}")
    for key in ("A_auto_diar", "B_pinned_diar", "C_auto_nodiar"):
        r = results["live"][key]
        if not r.get("connected"):
            print(f"[{key}] CONNECT FAILED: {r.get('error','')[:150]}")
            continue
        finals = r.get("finals", [])
        joined = " | ".join(t.replace("\n", " ")[:120] for t in finals)
        spk = any("spk_" in t or "[Speaker" in t or "Speaker 1" in t or "Speaker 2" in t for t in finals)
        print(
            f"[{key}] n_msgs={r.get('n_msgs')} finals={r.get('n_finals')} "
            f"interims={r.get('n_interims')} turn_complete={r.get('n_turn_complete')} "
            f"first_final_delay={r.get('first_final_delay_s')}s last_final_delay={r.get('last_final_delay_s')}s "
            f"SPEAKER_LABELS={spk}"
        )
        print(f"      finals: {joined[:400]}")
    print("\nDONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())