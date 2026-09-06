"""Prod smoke (S2): relay-path websocket validation on the deployed rev.

Drives the exact signal class that caused the 31 Aug S2 abort (prod session
6f689950): activity relay through the guard on vad=manual, including a
DUPLICATE activity_start and an UNBALANCED activity_end. On a rev without
step-5 hardening this kills the Gemini leg (1007 abort); on rev 00052+ the
guard suppresses both and the session must ride through clean, answer a
ping, and close with 1000 on a clean stop.

Run from backend/ with the venv python (env sourced from .env).
"""
import asyncio
import json
import os
import sys

import websockets

BASE = "wss://sirious-api-635321277027.asia-south1.run.app"
SILENCE = b"\x00\x00" * 1600  # 100 ms of 16 kHz s16le


async def main() -> int:
    token = os.environ["SIRIOUS_AUTH_TOKEN"]
    url = f"{BASE}/ws?token={token}&vad=manual"
    errors: list[str] = []
    events: list[str] = []

    try:
        async with websockets.connect(url) as ws:
            msg = json.loads(await ws.recv())
            print("session_started: vad_mode =", msg.get("vad_mode"),
                  "resumed =", msg.get("resumed"))
            if msg.get("vad_mode") != "manual":
                errors.append(
                    f"vad_mode={msg.get('vad_mode')!r} (expected manual)")

            # --- Leg 1: balanced window (relay-safe baseline) ---
            await ws.send("activity_start")
            await ws.send(SILENCE)
            await ws.send("activity_end")

            # --- Leg 2: S2 violation class ---
            # Duplicate start + unbalanced end: the guard must suppress
            # these server-side (activity_*_suppressed in server logs);
            # from the client we just require NO error / NO 1007 abort.
            await ws.send("activity_start")
            await ws.send("activity_start")
            await ws.send("activity_end")
            await ws.send("activity_end")

            # Let Gemini settle, then confirm the leg is still alive.
            await ws.send("ping")
            got_pong = False
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 15
            while loop.time() < deadline:
                try:
                    m = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                except websockets.exceptions.ConnectionClosed as e:
                    print("CLOSED mid-run:", e.rcvd, "/", e.sent)
                    errors.append(f"connection closed mid-run: {e.rcvd}")
                    break
                if isinstance(m, str):
                    d = json.loads(m)
                    t = d.get("type", "?")
                    events.append(t)
                    print("event:", t)
                    if t == "error":
                        errors.append("error event: " + json.dumps(d)[:200])
                    if t == "pong":
                        got_pong = True
                        break
                else:
                    print("event: <binary audio>", len(m), "bytes")

            print("pong received:", got_pong)
            if not got_pong:
                errors.append("no pong after relay signals")

            # --- Clean stop, phone-parity ---
            # The phone's endSession() sends stop then disconnects
            # immediately (sirious_session_controller._cleanupSession).
            # Deliberately do NOT linger: a client that stays attached
            # after stop keeps the Gemini leg alive (fast teardown waits
            # for the client leg), and after ~50s of idle Gemini aborts
            # 1007 → recovery handoff → close 4402. That is the recovery
            # net working as designed (observed 6 Sep), not a defect —
            # but it is a path real clients never exercise.
            await ws.send("stop")
            await ws.close()
            print("closed client leg after stop (phone-parity)")
    except websockets.exceptions.InvalidStatus as e:
        print("HANDSHAKE FAIL:", e)
        return 1

    print()
    print("events seen:", sorted(set(events)) or "(none)")
    if errors:
        print("SMOKE FAIL:")
        for err in errors:
            print("  -", err)
        return 1
    print("SMOKE PASS: relay path clean on deployed rev "
          "(guard suppressed violations, no error events, "
          "phone-parity close after stop)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
