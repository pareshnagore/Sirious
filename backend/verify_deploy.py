"""Post-deploy verification: health, auth gates, WS bridge, REST readback."""
import asyncio
import json
import sys

import httpx
import websockets

BASE = "https://sirious-api-635321277027.asia-south1.run.app"
TOKEN = sys.argv[1]


async def main() -> None:
    async with httpx.AsyncClient(timeout=20) as hc:
        r = await hc.get(f"{BASE}/health")
        print("health:", r.status_code, r.json())

        r = await hc.get(f"{BASE}/sessions")
        print("sessions no-token:", r.status_code, "(expect 401)")

        r = await hc.get(
            f"{BASE}/sessions", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        print("sessions with-token:", r.status_code, r.json())

    # WS without token must be rejected.
    try:
        wss_base = BASE.replace("https://", "wss://")
        async with websockets.connect(f"{wss_base}/ws") as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)
        print("ws no-token: UNEXPECTEDLY ACCEPTED")
        sys.exit(1)
    except websockets.exceptions.ConnectionClosed as e:
        print(f"ws no-token: rejected (code={e.rcvd.code if e.rcvd else '?'})")
    except Exception as e:
        print(f"ws no-token: rejected ({type(e).__name__})")

    # WS with token must open a real Gemini session.
    async with websockets.connect(
        f"{wss_base}/ws?token={TOKEN}&client_session_id=deploy-check"
    ) as ws:
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=30)
            if isinstance(msg, str):
                ev = json.loads(msg)
                print("ws event:", ev.get("type"))
                if ev.get("type") == "session_started":
                    print("resumed:", ev.get("resumed"))
                    await ws.send("ping")
                elif ev.get("type") == "pong":
                    print("PROD BRIDGE OK")
                    await ws.send("stop")
                    break

    # The deploy-check session should now exist in Firestore.
    await asyncio.sleep(3)
    async with httpx.AsyncClient(timeout=20) as hc:
        r = await hc.get(
            f"{BASE}/sessions", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        sessions = r.json().get("sessions", [])
        print("sessions after WS test:", r.status_code, len(sessions))
        for s in sessions:
            print("  -", s["id"], "turns:", s["turn_count"], "ended:", s["ended_at"] is not None)
        if sessions:
            sid = sessions[0]["id"]
            r = await hc.get(
                f"{BASE}/sessions/{sid}",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            print("detail:", r.status_code, "keys:", sorted(r.json().keys()))


asyncio.run(main())
