"""Deepgram smoke: batch + streaming diarization on the synthetic Hinglish WAV.
Run: set -a && source .env && set +a && python deepgram_smoke.py
Requires DEEPGRAM_KEY in env. Never prints the key.
"""

import asyncio
import json
import os
import sys

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
WAV = os.path.join(os.environ.get("LOCALAPPDATA", "/tmp"), "Temp", "sirious_hinglish.wav")
if not os.path.exists(WAV):
    WAV = os.path.join("/tmp", "sirious_hinglish.wav")

BASE = "https://api.deepgram.com/v1/listen"


def check_key() -> None:
    if not os.environ.get("DEEPGRAM_KEY"):
        print("DEEPGRAM_KEY not set — source backend/.env first")
        sys.exit(1)


def batch() -> None:
    params = {
        "model": "nova-3",
        "language": "multi",  # nova-3 multilingual: en<->hi code-switching
        "diarize": "true",
        "punctuate": "true",
        "smart_format": "true",
    }
    with open(WAV, "rb") as f:
        audio = f.read()
    r = httpx.post(
        BASE,
        params=params,
        content=audio,
        headers={
            "Authorization": f"Token {os.environ['DEEPGRAM_KEY']}",
            "Content-Type": "audio/wav",
        },
        timeout=120,
    )
    print(f"batch HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:300])
        return
    data = r.json()
    words = (
        data.get("results", {})
        .get("channels", [{}])[0]
        .get("alternatives", [{}])[0]
        .get("words", [])
    )
    speakers = sorted({w.get("speaker", 0) for w in words})
    print(f"words={len(words)} speakers={speakers}")
    cur, buf = None, []
    for w in words:
        s = w.get("speaker", 0)
        if s != cur:
            if buf:
                print(f"  S{cur}: {' '.join(buf)}")
            cur, buf = s, [w["word"]]
        else:
            buf.append(w["word"])
    if buf:
        print(f"  S{cur}: {' '.join(buf)}")


async def streaming() -> None:
    import websockets

    params = {
        "model": "nova-3",
        "language": "multi",
        "diarize": "true",
        "punctuate": "true",
        "smart_format": "true",
        "encoding": "linear16",
        "sample_rate": "16000",
        "channels": "1",
        "interim_results": "true",
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"wss://api.deepgram.com/v1/listen?{qs}"
    headers = {"Authorization": f"Token {os.environ['DEEPGRAM_KEY']}"}

    import wave

    with wave.open(WAV, "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        frames = w.readframes(w.getnframes())
    content = frames  # raw PCM, no RIFF header
    finals: list[str] = []
    speakers: set[int] = set()

    try:
        ws_ctx = websockets.connect(url, additional_headers=headers, max_size=10 * 1024 * 1024)
        ws = await ws_ctx.__aenter__()
        print("  [ws] connected")
    except websockets.InvalidStatus as e:
        body = b""
        try:
            body = e.response.body or b""
        except Exception:
            pass
        print(f"HANDSHAKE 400 body: {body.decode('utf-8', 'replace')[:400]}")
        raise SystemExit(1)

    async def sender():
        for i in range(0, len(content), 3200):
            await ws.send(content[i : i + 3200])
            await asyncio.sleep(0.03)
        await ws.send(json.dumps({"type": "Finalize"}))

    send_task = asyncio.create_task(sender())
    # receive loop in THIS coroutine (structure proven by the bisect probe);
    # sender runs concurrently as a task
    shown = 0
    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=15)
        except asyncio.TimeoutError:
            print("  [recv] idle 15s — done")
            break
        except websockets.ConnectionClosed as e:
            print(f"  [closed] {e.rcvd.code if e.rcvd else '?'} {e.rcvd.reason if e.rcvd else ''}")
            break
        data = json.loads(msg)
        if data.get("type") != "Results":
            continue
        if shown < 2:
            shown += 1
            print(f"  [wire {shown}] {str(data)[:180]}")
        alt = data.get("channel", {}).get("alternatives", [{}])[0]
        for w in alt.get("words", []):
            speakers.add(w.get("speaker", 0))
        if data.get("is_final") and alt.get("transcript"):
            finals.append(alt["transcript"])
    await send_task
    try:
        await ws.close()
    except Exception:
        pass

    print(f"\nstreaming: {len(finals)} finals, speakers={sorted(speakers)}")
    for t in finals:
        print(f"  - {t}")


def main() -> None:
    check_key()
    print(f"sample: {WAV} ({os.path.getsize(WAV)} bytes)")
    print("--- batch (nova-3, diarize, multi) ---")
    batch()
    print("\n--- streaming (nova-3, diarize, multi) ---")
    asyncio.run(streaming())


if __name__ == "__main__":
    main()
