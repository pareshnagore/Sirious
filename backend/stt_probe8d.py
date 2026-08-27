"""Probe 8d: full message inventory for gemini-3.5-transcribe-live.

Two runs on the 2-speaker fixture:
  D: diarization=True + client activity signals (auto activity detection OFF)
  E: diarization=True + auto detection ON, longer trailing silence

Every server message is printed with its kind + any text, so we see the REAL
message shape (model_turn? input_transcription? voice_activity?) instead of
assuming input_transcription is the only transcript channel.
"""

import asyncio
import os
import time
import wave

from google import genai
from google.genai import types

WAV = os.path.join(os.environ.get("LOCALAPPDATA", "/tmp"), "Temp", "sirious_hinglish.wav")
CHUNK = 3200


def load_pcm() -> bytes:
    with wave.open(WAV, "rb") as w:
        return w.readframes(w.getnframes())


def kind(m) -> str:
    if m.setup_complete:
        return "setup_complete"
    if m.server_content:
        sc = m.server_content
        bits = []
        if sc.model_turn:
            parts = sc.model_turn.parts or []
            txt = "".join(p.text or "" for p in parts if p.text)
            bits.append(f"model_turn(text={txt[:80]!r})")
        if sc.input_transcription and sc.input_transcription.text:
            bits.append(f"input_transcription({sc.input_transcription.text[:80]!r})")
        if sc.interim_input_transcription and sc.interim_input_transcription.text:
            bits.append(f"interim({sc.interim_input_transcription.text[:80]!r})")
        if sc.turn_complete:
            bits.append(f"turn_complete({sc.turn_complete_reason})")
        if sc.interrupted:
            bits.append("interrupted")
        if sc.generation_complete:
            bits.append("generation_complete")
        if sc.waiting_for_input:
            bits.append("waiting_for_input")
        if sc.interaction_status:
            bits.append(f"interaction_status={sc.interaction_status}")
        return "server_content[" + ", ".join(bits) + "]"
    if m.voice_activity:
        return f"voice_activity({m.voice_activity})"
    if m.session_resumption_update:
        return f"resumption(resumable={m.session_resumption_update.resumable})"
    if m.usage_metadata:
        return "usage_metadata"
    if m.go_away:
        return "go_away"
    if m.tool_call:
        return "tool_call"
    return repr(m)[:120]


async def run(client, label, cfg, use_activity_signals, silence_s) -> None:
    raw = load_pcm()
    print(f"\n===== RUN {label}: {cfg!r} | activity={use_activity_signals} silence={silence_s}s =====")
    t0 = time.perf_counter()
    try:
        async with client.aio.live.connect(model="gemini-3.5-transcribe-live", config=cfg) as s:
            print(f"[{time.perf_counter()-t0:6.2f}s] connected")
            if use_activity_signals:
                await s.send_realtime_input(activity_start=types.ActivityStart())
            for off in range(0, len(raw), CHUNK):
                await s.send_realtime_input(
                    audio=types.Blob(data=raw[off : off + CHUNK], mime_type="audio/pcm;rate=16000")
                )
            if use_activity_signals:
                await s.send_realtime_input(activity_end=types.ActivityEnd())
            for _ in range(int(silence_s * 10)):
                await s.send_realtime_input(
                    audio=types.Blob(data=b"\x00" * CHUNK, mime_type="audio/pcm;rate=16000")
                )
            print(f"[{time.perf_counter()-t0:6.2f}s] clip done; listening")
            try:
                async with asyncio.timeout(25):
                    async for m in s.receive():
                        print(f"[{time.perf_counter()-t0:6.2f}s] {kind(m)}")
            except TimeoutError:
                print(f"[{time.perf_counter()-t0:6.2f}s] <receive timeout>")
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: {e!r}")


def main() -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    cfg_d = types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(diarization=True),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        ),
    )
    cfg_e = types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(diarization=True),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                silence_duration_ms=500, prefix_padding_ms=200
            )
        ),
    )
    cfg_f = types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(diarization=True),
    )
    asyncio.run(run(client, "D_activity_signals", cfg_d, use_activity_signals=True, silence_s=2))
    asyncio.run(run(client, "E_aggressive_endpoint", cfg_e, use_activity_signals=False, silence_s=4))
    asyncio.run(run(client, "F_default", cfg_f, use_activity_signals=False, silence_s=6))
    print("\nDONE")


if __name__ == "__main__":
    main()