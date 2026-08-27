"""Phase 5 C+B E2E (local server on :8000): unified ambient+voice doc.

1. /ws/ambient with a fresh `amb-cb-*` client_session_id → stream the
   synthetic 2-speaker Hinglish fixture → collect diarized segments.
2. /ws (VOICE) with the SAME client_session_id + seed (room tail) + invoke
   text → no mic; Gemini answers from the invoke (C2 path). The backend
   should: treat this as a continuation of the ambient doc, replay the room
   turns as "S1: …" context (transcript_replay log), append the voice turn.
3. GET /sessions/<id> → assert ONE doc: mode=ambient, title = first room
   turn, turns = mixed ambient+voice kinds.

Run: set -a && source .env && set +a && python cb_e2e.py  (server up first)
"""

import asyncio
import json
import os
import sys
import wave

import httpx
import websockets

WAV = os.path.join(os.environ.get("LOCALAPPDATA", "/tmp"), "Temp", "sirious_hinglish.wav")


def _token() -> str:
    for line in open(os.path.join(os.path.dirname(__file__), ".env"), encoding="utf-8"):
        if line.startswith("SIRIOUS_AUTH_TOKEN="):
            return line.strip().split("=", 1)[1]
    raise SystemExit("SIRIOUS_AUTH_TOKEN missing in .env")


async def _ambient_leg(ws_url: str, token: str, doc_id: str) -> list[dict]:
    """Stream the fixture into /ws/ambient; return the emitted segments."""
    w = wave.open(WAV, "rb")
    frames = w.readframes(w.getnframes())
    w.close()
    url = f"{ws_url}/ws/ambient?token={token}&client_session_id={doc_id}"
    segments = []
    async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
        hello = json.loads(await ws.recv())
        assert hello.get("type") == "session_started", hello

        async def send():
            for i in range(0, len(frames), 3200):
                await ws.send(frames[i : i + 3200])
                await asyncio.sleep(0.03)
            await ws.send("stop")

        sender = asyncio.create_task(send())
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=20)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            data = json.loads(msg)
            if data.get("type") == "ambient_segment":
                segments.append(data)
                print(f"  ambient S{data['speaker']} [{data['start_s']:.1f}-{data['end_s']:.1f}] {data['text'][:60]}")
        await sender
    print(f"ambient leg: {len(segments)} segments")
    return segments


async def _voice_leg(ws_url: str, token: str, doc_id: str, seed: str, invoke: str) -> str:
    """Open /ws with the SAME id + seed + invoke; collect the spoken answer."""
    q = f"client_session_id={doc_id}&seed={seed}&invoke={invoke}&token={token}"
    url = f"{ws_url}/ws?{q}"
    answer = ""
    async with websockets.connect(url, max_size=10 * 1024 * 1024) as ws:
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=40)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            if isinstance(msg, bytes):
                continue
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue
            t = data.get("type")
            if t == "session_started":
                print(f"  voice session_started (id={data.get('session_id')})")
            elif t == "assistant_transcript":
                answer += data.get("text", "")
            elif t == "turn_complete":
                break
            elif t == "error":
                print(f"  voice ERROR: {data}")
                break
    print(f"voice leg: answer={answer[:120]!r}")
    return answer


async def main() -> int:
    token = _token()
    doc_id = f"amb-cb-e2e-{os.getpid()}-{int(asyncio.get_event_loop().time() * 1000) % 100000}"
    base = "http://127.0.0.1:8000"
    ws_url = "ws://127.0.0.1:8000"

    # 1) ambient leg
    segments = await _ambient_leg(ws_url, token, doc_id)
    if not segments:
        print("NO AMBIENT SEGMENTS — aborting (Deepgram down / bad key?)")
        return 2
    await asyncio.sleep(3)  # let the store writer flush

    # 2) voice leg, SAME client_session_id (the C+B unification)
    tail = [s for s in segments[:-1]]
    seed = "\n".join(f"S{s['speaker']}: {s['text']}" for s in tail)
    invoke = segments[-1]["text"] if "sirious" in segments[-1]["text"].lower() else "Sirious, can you answer that?"
    answer = await _voice_leg(ws_url, token, doc_id, seed, invoke)
    if not answer:
        print("NO VOICE ANSWER — aborting")
        return 2
    await asyncio.sleep(4)  # writer flush for the voice turn + end

    # 3) unified doc assertions
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{base}/sessions/{doc_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code != 200:
            print(f"GET /sessions/{doc_id}: HTTP {r.status_code}")
            return 2
        doc = r.json()
        kinds = [t.get("kind") for t in doc.get("turns", [])]
        print(f"\nUNIFIED DOC: mode={doc.get('mode')} title={doc.get('title')!r} turns={len(kinds)} kinds={kinds}")

        ok = True
        if doc.get("mode") != "ambient":
            print("  ✗ mode != ambient")
            ok = False
        if not doc.get("title"):
            print("  ✗ title empty — expected first room turn")
            ok = False
        if "ambient" not in kinds or "voice" not in kinds:
            print("  ✗ expected BOTH ambient and voice kinds")
            ok = False
        ambient_turns = [t for t in doc.get("turns", []) if t.get("kind") == "ambient"]
        voice_turns = [t for t in doc.get("turns", []) if t.get("kind") == "voice"]
        if not (len(ambient_turns) >= 2 and len(voice_turns) >= 1):
            print("  ✗ unexpected counts")
            ok = False
        if voice_turns and not voice_turns[-1].get("assistant_text"):
            print("  ✗ voice turn missing assistant_text")
            ok = False
        # clean up the probe doc (also strips any memory provenance)
        await client.delete(
            f"{base}/sessions/{doc_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        print(f"\n{'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))