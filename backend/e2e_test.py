"""E2E vs PROD: stream TTS question like a mic, assert turn persists to
Firestore and reads back via REST. Same shape as local_e2e.py."""
import asyncio
import json
import sys

import httpx
import websockets

BASE = "https://sirious-api-635321277027.asia-south1.run.app"
WSS = "wss://sirious-api-635321277027.asia-south1.run.app/ws"
TOKEN = sys.argv[1]
PCM = open("e2e_q2.pcm", "rb").read()
CID = "e2e-check"

user_text = ""
assistant_text = ""
done = asyncio.Event()


async def reader(ws):
    global assistant_text, user_text
    try:
        async for msg in ws:
            if isinstance(msg, str):
                ev = json.loads(msg)
                t = ev.get("type")
                if t == "user_transcript":
                    user_text += ev.get("text", "")
                    print("USER:", ev.get("text"), flush=True)
                elif t == "assistant_transcript":
                    assistant_text += ev.get("text", "")
                    print("ASST:", ev.get("text"), flush=True)
                elif t == "turn_complete":
                    print("TURN_COMPLETE", flush=True)
                    done.set()
                elif t == "session_started":
                    print("SESSION_STARTED resumed=", ev.get("resumed"), flush=True)
    except websockets.exceptions.ConnectionClosed:
        pass


async def main() -> None:
    async with websockets.connect(
        f"{WSS}?token={TOKEN}&client_session_id={CID}"
    ) as ws:
        rtask = asyncio.create_task(reader(ws))
        await asyncio.sleep(1.0)
        for i in range(0, len(PCM), 3200):
            await ws.send(PCM[i : i + 3200])
            await asyncio.sleep(0.1)
        print("QUESTION_STREAMED", flush=True)
        # Trailing silence — real mics keep streaming; Gemini's VAD needs it.
        silence = b"\x00" * 3200
        for _ in range(30):
            if done.is_set():
                break
            await ws.send(silence)
            await asyncio.sleep(0.1)
        try:
            await asyncio.wait_for(done.wait(), timeout=30)
        except asyncio.TimeoutError:
            print("TIMEOUT waiting for turn_complete")
        await ws.send("stop")
        await asyncio.sleep(3)  # let teardown flush session_end
        rtask.cancel()

    url = f"{BASE}/sessions/{CID}"
    async with httpx.AsyncClient(timeout=20) as hc:
        for attempt in range(10):
            r = await hc.get(url, headers={"Authorization": f"Bearer {TOKEN}"})
            d = r.json()
            if r.status_code == 200 and d.get("turn_count", 0) >= 1:
                print("\n=== PERSISTED ===")
                print(json.dumps(d, indent=2))
                turn = d["turns"][0]
                ok = (
                    "japan" in turn["user_text"].lower()
                    and len(turn["assistant_text"]) > 3
                )
                print("E2E_ASSERT:", "PASS" if ok else "FAIL")
                return
            await asyncio.sleep(3)
    print("E2E_ASSERT: FAIL — turn never appeared in Firestore")


asyncio.run(main())
