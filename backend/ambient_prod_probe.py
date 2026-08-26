"""Prod probe for the ambient endpoint (rev 00043+): stream the synthetic
Hinglish WAV into wss://...run.app/ws/ambient and count diarized segments.
"""

import asyncio
import json
import os
import sys
import wave

import websockets

WS = "wss://sirious-api-635321277027.asia-south1.run.app/ws/ambient"
WAV = os.path.join(os.environ.get("LOCALAPPDATA", "/tmp"), "Temp", "sirious_hinglish.wav")


async def main() -> None:
    token = ""
    for line in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), encoding="utf-8"):
        if line.startswith("SIRIOUS_AUTH_TOKEN="):
            token = line.strip().split("=", 1)[1]
    w = wave.open(WAV, "rb")
    frames = w.readframes(w.getnframes())
    w.close()

    url = f"{WS}?token={token}&client_session_id=prod-ambient-probe-{os.getpid()}"
    segments = []
    speakers = set()
    async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
        hello = json.loads(await ws.recv())
        print("hello:", hello)
        assert hello.get("type") == "session_started" and hello.get("mode") == "ambient"

        async def send():
            for i in range(0, len(frames), 3200):
                await ws.send(frames[i : i + 3200])
                await asyncio.sleep(0.03)
            await ws.send("stop")

        sender = asyncio.create_task(send())
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=25)
            except asyncio.TimeoutError:
                break
            except websockets.ConnectionClosed:
                break
            data = json.loads(msg)
            if data.get("type") == "ambient_segment":
                segments.append(data)
                speakers.add(data["speaker"])
                print(f"  S{data['speaker']}: {data['text']}")
        await sender

    print(f"\nPROD RESULT: {len(segments)} segments, speakers={sorted(speakers)}")
    assert len(segments) >= 6 and len(speakers) >= 2, "prod probe failed"


if __name__ == "__main__":
    asyncio.run(main())
