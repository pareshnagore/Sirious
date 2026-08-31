"""Headless probe v6: gap hypothesis — does a CONTINUOUS silence stream
(zeros, no dead air) keep manual-VAD windows alive across turns?

  A   window 1: start → speech → end → (silence streamed while waiting)
  B   window 2 (identical shape): does it transcribe + answer now?
  C   interrupt timing on a long answer (with silence kept flowing):
      cancel on new activity_start? barge-in speech transcribed?

Run: backend/.venv/Scripts/python.exe manual_vad_probe.py  (GEMINI_API_KEY)
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

MODEL = os.environ.get("SIRIOUS_MODEL", "gemini-3.1-flash-live-preview")
TMP = Path(os.environ.get("TEMP", "/tmp"))
CHUNK_MS = 100


def synth_speech_sync(text: str, base: str) -> bytes:
    wav = os.path.abspath(base + ".wav")
    pcm = base + ".pcm"
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{wav}'); "
        f"$s.Speak('{text}'); $s.Dispose();"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps], check=True, timeout=60
    )
    ff = subprocess.run(
        ["ffmpeg", "-y", "-i", wav, "-ar", "16000", "-ac", "1", "-f", "s16le",
         os.path.abspath(pcm)],
        capture_output=True,
    )
    if ff.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {ff.stderr[-400:].decode(errors='replace')}"
        )
    return Path(pcm).read_bytes()


async def feed_pcm(session, pcm: bytes) -> None:
    step = 3200
    for i in range(0, len(pcm), step):
        await session.send_realtime_input(
            audio=types.Blob(
                data=pcm[i : i + step], mime_type="audio/pcm;rate=16000"
            )
        )
        await asyncio.sleep(CHUNK_MS / 1000)


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, float]] = []
        self.t0 = time.monotonic()

    async def run(self, session) -> None:
        async for response in session.receive():
            sc = response.server_content
            if sc is None:
                continue
            t = time.monotonic() - self.t0
            if sc.input_transcription and sc.input_transcription.text:
                print(f"  [{t:6.2f}s] TXN {sc.input_transcription.text!r}")
                self._add("TXN", t)
            if sc.output_transcription and sc.output_transcription.text:
                print(f"  [{t:6.2f}s] OUT {sc.output_transcription.text[:50]!r}")
                self._add("OUT", t)
            if sc.model_turn and any(
                p.inline_data for p in (sc.model_turn.parts or [])
            ):
                self._add("AUDIO", t)
            if sc.interrupted:
                self._add("INTERRUPTED", t)
            if sc.generation_complete:
                self._add("GENERATION_COMPLETE", t)
            if sc.turn_complete:
                self._add("TURN_COMPLETE", t)

    def _add(self, label: str, t: float) -> None:
        if not self.events or self.events[-1][0] != label:
            self.events.append((label, t))
            print(f"  [{t:6.2f}s] ** {label}")

    async def wait_for(self, labels, n0: int, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(e in labels for e, _ in self.events[n0:]):
                return True
            await asyncio.sleep(0.05)
        return False


def names(events) -> list[str]:
    return [e for e, _ in events]


async def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY missing", file=sys.stderr)
        return

    client = genai.Client(api_key=api_key)
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=True,
            ),
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    async with client.aio.live.connect(model=MODEL, config=config) as session:
        sink = Sink()
        receiver = asyncio.create_task(sink.run(session))
        print(f"== {MODEL} — manual VAD probe v6 (continuous silence) ==\n")

        async def silence_while(labels: tuple, n0: int, timeout: float) -> bool:
            """Wait for labels WHILE streaming silence (no dead air)."""
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if any(e in labels for e, _ in sink.events[n0:]):
                    return True
                await session.send_realtime_input(
                    audio=types.Blob(
                        data=b"\x00\x00" * 1600,
                        mime_type="audio/pcm;rate=16000",
                    )
                )
                await asyncio.sleep(CHUNK_MS / 1000)
            return False

        # ── A: window 1 ─────────────────────────────────────────────────────
        print("A: start → speech → end; silence STREAMED while waiting")
        n0 = len(sink.events)
        await session.send_realtime_input(activity_start=types.ActivityStart())
        await feed_pcm(
            session,
            synth_speech_sync(
                "What is two plus two? One word answer.", str(TMP / "vadq")
            ),
        )
        await session.send_realtime_input(activity_end=types.ActivityEnd())
        a_ok = await silence_while(("TURN_COMPLETE",), n0, 12.0)
        evs_a = sink.events[n0:]
        print(f"    → turn_complete: {a_ok}   events: {names(evs_a) or 'NONE'}\n")

        # ── B: window 2, same shape, still no dead air ─────────────────────
        print("B: window 2 (no gap anywhere)")
        n1 = len(sink.events)
        await session.send_realtime_input(activity_start=types.ActivityStart())
        await feed_pcm(
            session,
            synth_speech_sync(
                "What is the capital of France? One word answer.",
                str(TMP / "vadcap"),
            ),
        )
        await session.send_realtime_input(activity_end=types.ActivityEnd())
        b_ok = await silence_while(("TURN_COMPLETE",), n1, 12.0)
        evs_b = sink.events[n1:]
        print(f"    → turn_complete: {b_ok}   events: {names(evs_b) or 'NONE'}\n")

        # ── C: interrupt timing ─────────────────────────────────────────────
        d_start = d_end = d_txn = d_turn = None
        if b_ok:
            print("C: count-to-twenty; wait for AUDIO (silence flowing); interrupt")
            n2 = len(sink.events)
            await session.send_realtime_input(
                activity_start=types.ActivityStart()
            )
            await feed_pcm(
                session,
                synth_speech_sync(
                    "Count slowly from one to five, one number per second.",
                    str(TMP / "vadcount"),
                ),
            )
            await session.send_realtime_input(activity_end=types.ActivityEnd())
            got_audio = await silence_while(("AUDIO",), n2, 15.0)
            print(f"    → fresh audio: {got_audio}")
            if got_audio:
                await asyncio.sleep(2.0)  # answer plays; keep stream alive
                for _ in range(10):
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=b"\x00\x00" * 1600,
                            mime_type="audio/pcm;rate=16000",
                        )
                    )
                    await asyncio.sleep(CHUNK_MS / 1000)
                t_s = time.monotonic() - sink.t0
                print("    → NEW activity_start + barge-in speech …")
                await session.send_realtime_input(
                    activity_start=types.ActivityStart()
                )
                await feed_pcm(
                    session,
                    synth_speech_sync(
                        "Stop. What is the capital of Japan?",
                        str(TMP / "vadjapan"),
                    ),
                )
                await asyncio.sleep(1.0)
                after_start = [
                    e for e, ts in sink.events[n2:] if ts >= t_s
                ]
                print(f"      events so far: {after_start or 'NONE'}")
                t_e = time.monotonic() - sink.t0
                await session.send_realtime_input(
                    activity_end=types.ActivityEnd()
                )
                c_ok = await silence_while(
                    ("TURN_COMPLETE", "GENERATION_COMPLETE"), n2, 12.0
                )
                after_end = [
                    e for e, ts in sink.events[n2:]
                    if ts >= t_e and e not in after_start
                ]
                print(f"    → after end: {after_end or 'NONE'}   settled: {c_ok}")
                d_start = "INTERRUPTED" in after_start
                d_end = "INTERRUPTED" in after_end
                d_txn = any(
                    e == "TXN" and ts >= t_s for e, ts in sink.events[n2:]
                )
                d_turn = "TURN_COMPLETE" in after_start + after_end

        print("\n== VERDICT ==")
        print(f"A  window 1 with continuous silence: turn_complete={a_ok}")
        print(f"B  window 2 with continuous silence: turn_complete={b_ok if 'b_ok' in dir() else '?'}")
        if d_start is not None:
            print(f"D  cancel on new activity_start: {d_start}")
            print(f"D  cancel only on activity_end:  {d_end}")
            print(f"D  barge-in speech transcribed:  {d_txn}")
            print(f"D  turn_complete after interrupt: {d_turn}")

        receiver.cancel()
        try:
            await receiver
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
