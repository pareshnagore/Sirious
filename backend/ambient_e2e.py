"""E2E for /ws/ambient (local server): stream the synthetic Hinglish WAV as
binary frames, assert diarized segments arrive over the socket, and print
what Firestore would have received. Run with the server up on :8000.
"""

import asyncio
import json
import os
import sys
import wave

import httpx
import websockets

WS = "ws://127.0.0.1:8000/ws/ambient"
WAV = os.path.join(os.environ.get("LOCALAPPDATA", "/tmp"), "Temp", "sirious_hinglish.wav")


async def main() -> None:
    token = ""
    for line in open(os.path.join(os.path.dirname(__file__), ".env"), encoding="utf-8"):
        if line.startswith("SIRIOUS_AUTH_TOKEN="):
            token = line.strip().split("=", 1)[1]
    w = wave.open(WAV, "rb")
    frames = w.readframes(w.getnframes())
    w.close()

    url = f"{WS}?token={token}&client_session_id=e2e-ambient-{os.getpid()}"
    segments = []
    async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
        hello = json.loads(await ws.recv())
        print("hello:", hello)
        assert hello.get("type") == "session_started"

        async def send():
            for i in range(0, len(frames), 3200):
                await ws.send(frames[i : i + 3200])
                await asyncio.sleep(0.03)
            await ws.send("stop")

        sender = asyncio.create_task(send())
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=20)
            except asyncio.TimeoutError:
                break
            except websockets.ConnectionClosed:
                break
            data = json.loads(msg)
            if data.get("type") == "ambient_segment":
                segments.append(data)
                print(f"  S{data['speaker']} [{data['start_s']:.1f}-{data['end_s']:.1f}] {data['text']}")
            elif data.get("type") == "pong":
                pass
        await sender

    speakers = sorted({s["speaker"] for s in segments})
    print(f"\nRESULT: {len(segments)} segments over WS, speakers={speakers}")
    if segments:
        # verify persistence landed in Firestore via the REST API
        await asyncio.sleep(3)  # let the store writer flush
        doc_id = url.split("client_session_id=")[1]
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"http://127.0.0.1:8000/sessions/{doc_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                doc = r.json()
                turns = doc.get("turns", [])
                kinds = {t.get("kind") for t in turns}
                spk = sorted({t.get("speaker") for t in turns})
                print(f"FIRESTORE: mode={doc.get('mode')} turns={len(turns)} kinds={kinds} speakers={spk}")
            else:
                print(f"FIRESTORE read failed: HTTP {r.status_code} (SIRIOUS_PERSIST off locally?)")
    assert len(segments) >= 6, "expected at least 6 diarized segments"
    assert len(speakers) >= 2, "expected >=2 speakers"


if __name__ == "__main__":
    asyncio.run(main())
