"""Manual smoke test for Phase 2: auth + WS bridge + REST history API.

Usage:
  1. Start the server (see sirious-build skill / README):
       set -a && source ./.env && set +a
       SIRIOUS_AUTH_TOKEN=localtoken .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
  2. Run:  .venv/Scripts/python.exe smoke_test.py ws://127.0.0.1:8000 <token>
"""

import asyncio
import json
import sys

import httpx
import websockets


async def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8000"
    token = sys.argv[2] if len(sys.argv) > 2 else ""

    http_base = base.replace("ws", "http", 1)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async with httpx.AsyncClient(timeout=10) as hc:
        r = await hc.get(f"{http_base}/health")
        print("health:", r.status_code, r.json())

        r = await hc.get(f"{http_base}/sessions")
        print("sessions no-token:", r.status_code, "(expect 401)" if token else "(expect 200)")

        if token:
            r = await hc.get(f"{http_base}/sessions", headers=headers)
            print("sessions with-token:", r.status_code, r.json())

    ws_url = f"{base}/ws?client_session_id=smoke-{asyncio.get_running_loop().time():.0f}"
    if token:
        ws_url += f"&token={token}"

    print("connecting:", ws_url.split("&")[0], "…")
    async with websockets.connect(ws_url) as ws:
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=30)
            if isinstance(msg, str):
                event = json.loads(msg)
                print("event:", event.get("type"))
                if event.get("type") == "session_started":
                    await ws.send("ping")
                elif event.get("type") == "pong":
                    print("PING/PONG OK — full bridge verified")
                    await ws.send("stop")
                    break
                elif event.get("type") == "error":
                    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
