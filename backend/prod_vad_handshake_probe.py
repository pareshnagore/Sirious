"""Local handshake probe: verify vad=hybrid / vad=manual / default shapes.

Connects to a LOCAL server, checks the session_started vad_mode and that
the hybrid connection stays alive across a ping. No audio assertions —
the Live-session VAD config is exercised for real only on device.
"""
import asyncio
import json
import os
import sys

import websockets

BASE = "ws://127.0.0.1:8765"


async def probe(query: str, expect_vad: str) -> bool:
    url = f"{BASE}/ws?token={os.environ['SIRIOUS_AUTH_TOKEN']}{query}"
    try:
        async with websockets.connect(url) as ws:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
            vad = msg.get("vad_mode")
            ok = vad == expect_vad
            print(f"{query or '(default)':14} -> vad_mode={vad!r} "
                  f"{'OK' if ok else 'MISMATCH (expected ' + expect_vad + ')'}")
            if not ok:
                return False
            await ws.send("ping")
            while True:
                m = await asyncio.wait_for(ws.recv(), timeout=20)
                if isinstance(m, str) and json.loads(m).get("type") == "pong":
                    print("    pong OK")
                    return True
    except Exception as e:  # noqa: BLE001
        print(f"{query or '(default)':14} -> ERROR {e!r}")
        return False


async def main() -> int:
    results = [
        await probe("&vad=hybrid", "hybrid"),
        await probe("&vad=manual", "manual"),
        await probe("", "server"),
    ]
    print("ALL OK" if all(results) else "FAILURES PRESENT")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
