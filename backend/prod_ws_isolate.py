"""Isolate the prod WS 502: try plain handshake (no vad param) vs vad=manual.
"""
import asyncio
import json
import os

import websockets

BASE = "wss://sirious-api-635321277027.asia-south1.run.app"


async def try_connect(label: str, query: str) -> None:
    url = f"{BASE}/ws?token={os.environ['SIRIOUS_AUTH_TOKEN']}{query}"
    try:
        async with websockets.connect(url, open_timeout=15) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            print(f"connect{query!r:12} OK  vad_mode={msg.get('vad_mode')}")
            await ws.send("stop")
    except Exception as e:  # noqa: BLE001
        print(f"connect{query!r:12} FAILED: {type(e).__name__}: {e}")


async def main() -> None:
    await asyncio.sleep(3)
    await main_once()


async def main_once() -> None:
    await try_connect("")


async def try_connect(query: str) -> None:
    url = f"{BASE}/ws?token={os.environ['SIRIOUS_AUTH_TOKEN']}{query}"
    try:
        async with websockets.connect(url) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            print(f"connect{query!r:12} OK  vad_mode={msg.get('vad_mode')}")
            await ws.send("stop")
    except Exception as e:  # noqa: BLE001
        print(f"connect{query!r:12} FAILED: {type(e).__name__}: {e}")


async def main_all() -> None:
    # plain (old path)
    await try_connect("")
    await asyncio.sleep(3)
    # vad=manual
    await try_connect("&vad=manual")


if __name__ == "__main__":
    asyncio.run(main_all())
