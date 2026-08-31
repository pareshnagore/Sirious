"""Prod smoke: vad=manual handshake + activity relay on the deployed rev.

Run from backend/ with the venv python (env sourced from .env).
"""
import asyncio
import json
import os
import sys

import websockets

BASE = "wss://sirious-api-635321277027.asia-south1.run.app"


async def main() -> None:
    token = os.environ["SIRIOUS_AUTH_TOKEN"]
    url = f"{BASE}/ws?token={token}&vad=manual"
    async with websockets.connect(url) as ws:
        msg = json.loads(await ws.recv())
        print("session_started:", "vad_mode =", msg.get("vad_mode"),
              "resumed =", msg.get("resumed"))
        await ws.send("activity_start")
        await ws.send(b"\x00\x00" * 1600)
        await ws.send("activity_end")
        await ws.send("ping")
        got_pong = False
        for _ in range(5):
            try:
                m = await asyncio.wait_for(ws.recv(), timeout=8)
            except asyncio.TimeoutError:
                break
            if isinstance(m, str):
                d = json.loads(m)
                t = d.get("type")
                print("event:", t)
                if t == "error":
                    print("  ERROR:", d.get("message"))
                if t == "pong":
                    got_pong = True
            else:
                print("event: <binary audio>", len(m), "bytes")
        await ws.send("stop")
        print("pong received:", got_pong)


if __name__ == "__main__":
    asyncio.run(main())
