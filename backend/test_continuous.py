import asyncio
import sounddevice as sd
import websockets


URI = "wss://sirious-api-635321277027.asia-south1.run.app/ws"

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_MS = 100
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000


async def microphone(ws):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def callback(indata, frames, time, status):
        if status:
            print("Audio:", status)
        data = indata.copy().tobytes()
        loop.call_soon_threadsafe(queue.put_nowait, data)

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        blocksize=CHUNK_SAMPLES,
        callback=callback,
    ):
        while True:
            data = await queue.get()
            await ws.send(data)


async def speaker(ws):
    stream = sd.RawOutputStream(
        samplerate=24000,
        channels=1,
        dtype="int16",
        blocksize=1920,
    )
    stream.start()

    try:
        while True:
            data = await ws.recv()

            if isinstance(data, bytes):
                stream.write(data)

    finally:
        stream.stop()
        stream.close()


async def main():
    print("Connecting...")

    async with websockets.connect(
        URI,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:

        print("Connected.")
        print("Speak normally. Press Ctrl+C to stop.")

        mic_task = asyncio.create_task(microphone(ws))
        speaker_task = asyncio.create_task(speaker(ws))

        try:
            await asyncio.gather(mic_task, speaker_task)
        finally:
            mic_task.cancel()
            speaker_task.cancel()


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nStopped.")