#!/usr/bin/env python3
"""Sirious live-session smoke test.

Connects to a Sirious WebSocket endpoint, streams a short synthesized burst
of 16 kHz int16 PCM, sends the "stop" control, then collects frames until
the socket closes or the timeout elapses. Prints a JSON summary and saves
assistant audio to the repo root as smoke_response.wav (24 kHz mono).

Depends only on `websockets` (already present transitively via uvicorn in
the backend venv). Run with the backend interpreter:

  cd backend && .venv/bin/python .claude/skills/realtime-session-test/run_smoke.py \
      --ws ws://localhost:8080/ws
      # or --ws wss://sirious-api-635321277027.asia-south1.run.app/ws
"""
from __future__ import annotations

import argparse
import array
import asyncio
import json
import math
import wave
from pathlib import Path

import websockets

MIC_SR = 16000
OUT_SR = 24000


async def run(ws_url: str, wait_s: float, timeout_s: float) -> None:
    async with websockets.connect(ws_url, ping_interval=None) as ws:
        # ~0.5 s of soft 220 Hz tone at 16 kHz mono int16.
        burst = array.array("h")
        for i in range(int(MIC_SR * 0.5)):
            burst.append(int(8000 * math.sin(2 * math.pi * 220 * i / MIC_SR)))

        await ws.send(burst.tobytes())
        await ws.send("stop")  # control command per protocol

        audio = bytearray()
        text_events: list[str] = []
        binary_frames = 0
        server_closed_early = True

        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    frame = await ws.recv()
                    if isinstance(frame, bytes):
                        binary_frames += 1
                        audio += frame
                    else:
                        text_events.append(frame)
        except asyncio.TimeoutError:
            server_closed_early = False  # we timed out waiting; socket still up
        except websockets.ConnectionClosed:
            pass  # server closed normally — not "early" in a failing sense

        summary = {
            "target": ws_url,
            "binary_frames": binary_frames,
            "text_events": len(text_events),
            "audio_bytes": len(audio),
            "audio_seconds_at_24k": round(len(audio) / (OUT_SR * 2), 2),
            "server_closed_early": server_closed_early,
            "first_text_event": text_events[0] if text_events else None,
        }
        print(json.dumps(summary, indent=2), flush=True)

        if audio:
            out = Path(__file__).resolve().parent.parent.parent / "smoke_response.wav"
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(OUT_SR)
                w.writeframes(bytes(audio))
            print(f"saved {out}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Sirious WebSocket smoke test")
    ap.add_argument("--ws", required=True)
    ap.add_argument("--timeout", type=float, default=40.0)
    args = ap.parse_args()
    asyncio.run(run(args.ws, wait_s=0.5, timeout_s=args.timeout))


if __name__ == "__main__":
    main()