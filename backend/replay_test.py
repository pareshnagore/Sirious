"""Live validation of the transcript-replay fallback against PROD.

Conversation 1: tell Sirious a fact, end cleanly (clean stop DROPS the
in-memory resumption handle by design).
Connection 2 (same client_session_id, no live handle): ask about the fact.
If Sirious answers correctly, the model got its memory from the Firestore
transcript injected into system_instruction -> replay fallback WORKS.
"""
import asyncio
import json
import sys

import httpx
import websockets

WSS = "wss://sirious-api-635321277027.asia-south1.run.app/ws"
TOKEN = sys.argv[1]
CID = "replay-test"
PCM_Q = "replay_q.pcm"
PCM_A = "replay_a.pcm"


async def converse(pcm_path: str, max_silence_frames: int = 40) -> tuple[str, str]:
    """Stream pcm as mic, return (user_text, assistant_text)."""
    pcm = open(pcm_path, "rb").read()
    user = ""
    asst = ""
    done = asyncio.Event()

    async def reader(ws):
        nonlocal user, asst
        try:
            async for m in ws:
                if isinstance(m, str):
                    ev = json.loads(m)
                    if ev["type"] == "user_transcript":
                        user += ev["text"]
                    elif ev["type"] == "assistant_transcript":
                        asst += ev["text"]
                        print("   ASST:", ev["text"], flush=True)
                    elif ev["type"] == "turn_complete":
                        done.set()
        except websockets.exceptions.ConnectionClosed:
            pass

    async with websockets.connect(
        f"{WSS}?token={TOKEN}&client_session_id={CID}"
    ) as ws:
        rt = asyncio.create_task(reader(ws))
        await asyncio.sleep(1)
        for i in range(0, len(pcm), 3200):
            await ws.send(pcm[i : i + 3200])
            await asyncio.sleep(0.1)
        silence = b"\x00" * 3200
        for _ in range(max_silence_frames):
            if done.is_set():
                break
            await ws.send(silence)
            await asyncio.sleep(0.1)
        if not done.is_set():
            print("   (no turn_complete — waiting 15s)")
            try:
                await asyncio.wait_for(done.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
        await ws.send("stop")
        await asyncio.sleep(2)
        rt.cancel()
    return user.strip(), asst.strip()


async def main() -> None:
    # ── Conversation 1: state the fact ────────────────────────────────────
    print("[1] Telling Sirious the fact…", flush=True)
    u1, a1 = await converse(PCM_Q)
    print(f"[1] user='{u1}'\n[1] asst='{a1}'", flush=True)

    # Confirm turn 1 persisted before reconnecting.
    async with httpx.AsyncClient(timeout=20) as hc:
        r = await hc.get(
            f"https://sirious-api-635321277027.asia-south1.run.app/sessions/{CID}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        d = r.json()
        print(f"[1] persisted turns={d.get('turn_count')}", flush=True)
        assert d.get("turn_count", 0) >= 1, "fact turn did not persist!"

    # ── Conversation 2: same id, handle gone (clean stop dropped it) ──────
    print("[2] Reconnecting (fresh Gemini session) and asking…", flush=True)
    u2, a2 = await converse(PCM_A)
    print(f"[2] user='{u2}'\n[2] asst='{a2}'", flush=True)

    combined = a2.lower()
    ok = ("teal" in combined) or ("turquoise" in combined)
    print("\nREPLAY_ASSERT:", "PASS — remembered via transcript replay" if ok
          else "FAIL — did not remember")


asyncio.run(main())
