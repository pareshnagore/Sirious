"""Phase 5 C2 E2E: /ws with seed+invoke -> audio answer, local server.

Proves the invocation mechanism end-to-end WITHOUT the phone:
- connect /ws?seed=<room tail>&invoke=<trigger text>
- expect session_started -> assistant audio frames -> assistant_transcript
  -> turn_complete, all WITHOUT streaming any mic audio (the room question
  rides the invoke text, not the mic).

Usage (from backend/):
    set -a && source ./.env && set +a
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000   (separate shell)
    .venv/Scripts/python.exe invocation_e2e.py
"""

import asyncio
import json
import os
import sys

import websockets

WS_URL = os.environ.get("SIRIOUS_WS_URL", "ws://127.0.0.1:8000/ws")

SEED = (
    "S1: What is the longest train in the world?\n"
    "S2: I think it might be in Australia, actually.\n"
    "S1: Sirious would know."
)
INVOKE = "Sirious, can you answer that?"


async def main() -> None:
    token = os.environ.get("SIRIOUS_AUTH_TOKEN", "")
    qs = f"?token={token}"
    # URL-encode the seed/invoke the way Uri.replace does on the phone:
    # websockets takes a plain string; encode via urllib.
    from urllib.parse import urlencode

    qs += "&" + urlencode({"seed": SEED, "invoke": INVOKE})
    qs += "&client_session_id=inv-e2e-" + str(int(asyncio.get_event_loop().time() * 1000))

    audio_bytes = 0
    assistant_text = ""
    saw_started = False
    saw_complete = False

    async with websockets.connect(WS_URL + qs, max_size=64 * 1024 * 1024) as ws:
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=60)
            if isinstance(msg, (bytes, bytearray)):
                audio_bytes += len(msg)
                continue
            data = json.loads(msg)
            t = data.get("type")
            print(f"EVENT {t} {str(data)[:150]}")
            if t == "session_started":
                saw_started = True
            elif t == "assistant_transcript":
                assistant_text += data.get("text", "")
            elif t == "turn_complete":
                saw_complete = True
                break
            elif t == "error":
                print("SERVER ERROR:", data)
                return 1
            elif t == "session_warning":
                print("SERVER WARNING:", data.get("code"), data.get("time_left"))

    print("\n=== RESULT ===")
    print(f"session_started:  {saw_started}")
    print(f"audio bytes:      {audio_bytes}")
    print(f"turn_complete:    {saw_complete}")
    print(f"assistant text:   {assistant_text[:400]!r}")
    if not (saw_started and audio_bytes > 0 and assistant_text.strip() and saw_complete):
        print("FAIL: invocation did not produce a full spoken answer")
        return 1
    print("PASS: invoke -> spoken answer without mic audio")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))