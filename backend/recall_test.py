"""Phase 3 NORTH-STAR recall test (Paresh's objective, agreed 22 Aug 2026).

    Session N:   "What is the color of a peacock?"     (casual conversation)
    Session N+1: "Did I have any conversation about birds?"
    Sirious:     "Yes — you talked about peacocks …"

Runs against any deployment:
    python recall_test.py <token>                       # PROD
    BASE=http://127.0.0.1:8000 WSS=ws://127.0.0.1:8000/ws \
        python recall_test.py ""                        # local server

Design notes
------------
- Unique client_session_ids per run: session N+1 gets a FRESH doc, so the
  Phase 2 replay fallback (same-doc reconnects) cannot explain a pass —
  only cross-session MEMORY injection can.
- Between N and N+1 the test polls GET /memories until a peacock-related
  memory carrying session N's provenance exists ⇒ extraction completed.
- Requires: edge-tts + ffmpeg on PATH (audio generated if .pcm missing),
  SIRIOUS_MEMORY=1 on the target deployment.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid

import httpx
import websockets

BASE = os.environ.get("BASE", "https://sirious-api-635321277027.asia-south1.run.app")
WSS = os.environ.get("WSS", "wss://sirious-api-635321277027.asia-south1.run.app/ws")
TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""

RUN = uuid.uuid4().hex[:8]
CID_N = f"recall-peacock-{RUN}"
CID_N1 = f"recall-birds-{RUN}"

Q_PEACOCK = "What is the color of a peacock?"
Q_BIRDS = "Did I have any conversation about birds?"

PCM_N = f"recall_n_{RUN}.pcm"
PCM_N1 = f"recall_n1_{RUN}.pcm"


def tts_to_pcm(text: str, out_path: str) -> None:
    """edge-tts → mp3 → ffmpeg → 16 kHz mono s16le pcm (proven recipe)."""
    mp3 = out_path[:-4] + ".mp3"
    subprocess.run(
        ["edge-tts", "--voice", "en-US-AndrewMultilingualNeural",
         "--text", text, "--write-media", mp3],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", mp3, "-ar", "16000", "-ac", "1",
         "-f", "s16le", out_path],
        check=True, capture_output=True,
    )
    os.remove(mp3)


async def converse(pcm_path: str, cid: str) -> tuple[str, str]:
    """Stream pcm as mic over WS; return (user_text, assistant_text)."""
    with open(pcm_path, "rb") as f:
        pcm = f.read()
    user = ""
    asst = ""
    done = asyncio.Event()

    async def reader(ws):
        nonlocal user, asst
        try:
            async for m in ws:
                if isinstance(m, str):
                    ev = json.loads(m)
                    t = ev.get("type")
                    if t == "session_started":
                        print(f"   [{cid}] session_started resumed={ev.get('resumed')}",
                              flush=True)
                    elif t == "user_transcript":
                        user += ev.get("text", "")
                    elif t == "assistant_transcript":
                        asst += ev.get("text", "")
                        print(f"   ASST: {ev.get('text')}", flush=True)
                    elif t == "turn_complete":
                        done.set()
        except websockets.exceptions.ConnectionClosed:
            pass

    url = f"{WSS}?client_session_id={cid}"
    if TOKEN:
        url += f"&token={TOKEN}"
    async with websockets.connect(url) as ws:
        rt = asyncio.create_task(reader(ws))
        await asyncio.sleep(1)
        for i in range(0, len(pcm), 3200):
            await ws.send(pcm[i : i + 3200])
            await asyncio.sleep(0.1)
        silence = b"\x00" * 3200          # keep the mic open (Gemini VAD)
        for _ in range(40):
            if done.is_set():
                break
            await ws.send(silence)
            await asyncio.sleep(0.1)
        if not done.is_set():
            print("   (no turn_complete — waiting 15s)", flush=True)
            try:
                await asyncio.wait_for(done.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
        await ws.send("stop")
        await asyncio.sleep(2)            # let teardown flush + enqueue extract
        rt.cancel()
    return user.strip(), asst.strip()


async def headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


async def wait_for_persisted(cid: str, hc: httpx.AsyncClient) -> None:
    h = await headers()
    for _ in range(20):
        r = await hc.get(f"{BASE}/sessions/{cid}", headers=h)
        if r.status_code == 200 and r.json().get("turn_count", 0) >= 1:
            print(f"[persist] {cid} ok", flush=True)
            return
        await asyncio.sleep(3)
    raise AssertionError(f"session {cid} never persisted")


async def wait_for_extraction(hc: httpx.AsyncClient, timeout_s: int = 180) -> dict:
    """Poll /memories until a peacock-hit carries session N's provenance."""
    h = await headers()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = await hc.get(
            f"{BASE}/memories", params={"q": "peacock bird"}, headers=h
        )
        if r.status_code == 200:
            for m in r.json().get("memories", []):
                prov_refs = {
                    p.get("session_ref") for p in (m.get("provenance") or [])
                }
                if CID_N in prov_refs:
                    print(f"[extract] memory ready: {m.get('text')!r}", flush=True)
                    return m
        await asyncio.sleep(5)
    raise AssertionError("extraction never produced session N's memory")


async def main() -> None:
    for p in (PCM_N, PCM_N1):
        if not os.path.exists(p):
            tts_to_pcm(Q_PEACOCK if p == PCM_N else Q_BIRDS, p)
    async with httpx.AsyncClient(timeout=30) as hc:
        # ── Session N: casual peacock question ────────────────────────────
        print(f"[N] {Q_PEACOCK}", flush=True)
        u1, _ = await converse(PCM_N, CID_N)
        print(f"[N] user='{u1}'", flush=True)
        assert "peacock" in u1.lower(), "transcription mismatch"
        await wait_for_persisted(CID_N, hc)

        # ── Wait for the extraction job ───────────────────────────────────
        mem = await wait_for_extraction(hc)

        # ── Session N+1: DIFFERENT doc — replay fallback can't help here ──
        print(f"[N+1] {Q_BIRDS}", flush=True)
        u2, a2 = await converse(PCM_N1, CID_N1)
        print(f"[N+1] user='{u2}'", flush=True)

    combined = a2.lower()
    ok = "peacock" in combined and (
        "yes" in combined or "did" in combined or "talked" in combined
    )
    print("\nRECALL_ASSERT:", (
        "PASS — recalled the act of discussing, with memory:\n"
        f"  {mem.get('text')!r}\n  answer: {a2!r}"
    ) if ok else f"FAIL — answer was: {a2!r}")

    print("\nCleanup hints:")
    print(f"  sessions: {CID_N}, {CID_N1}")
    print(f"  delete via: DELETE /memories/{{id}} if unwanted")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
