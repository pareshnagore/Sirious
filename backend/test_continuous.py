import asyncio
import sounddevice as sd
import websockets
from websockets.exceptions import ConnectionClosed


URI = "wss://sirious-api-635321277027.asia-south1.run.app/ws"

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_MS = 100
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000

OUTPUT_SAMPLE_RATE = 24000
OUTPUT_BLOCKSIZE = 1920


async def microphone(ws, stop_event):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def callback(indata, frames, time, status):
        if status:
            print("Audio:", status)

        if stop_event.is_set():
            return

        data = indata.copy().tobytes()

        try:
            loop.call_soon_threadsafe(queue.put_nowait, data)
        except RuntimeError:
            # Event loop has already shut down.
            pass

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        blocksize=CHUNK_SAMPLES,
        callback=callback,
    ):
        while not stop_event.is_set():
            try:
                data = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            if stop_event.is_set():
                break

            try:
                await ws.send(data)
            except ConnectionClosed:
                break


async def speaker(ws, stop_event):
    stream = sd.RawOutputStream(
        samplerate=OUTPUT_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=OUTPUT_BLOCKSIZE,
    )

    stream.start()

    try:
        while not stop_event.is_set():
            try:
                data = await ws.recv()
            except ConnectionClosed:
                break

            if isinstance(data, bytes) and data:
                stream.write(data)

    finally:
        stream.stop()
        stream.close()


async def main():
    print("Connecting...")

    stop_event = asyncio.Event()

    try:
        async with websockets.connect(
            URI,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ) as ws:

            print("Connected.")
            print("Speak normally. Press Ctrl+C to stop.")

            mic_task = asyncio.create_task(
                microphone(ws, stop_event)
            )
            speaker_task = asyncio.create_task(
                speaker(ws, stop_event)
            )

            try:
                # Whichever task finishes first ends the session.
                done, pending = await asyncio.wait(
                    [mic_task, speaker_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Surface unexpected exceptions from the completed task.
                for task in done:
                    exception = task.exception()
                    if exception:
                        raise exception

            finally:
                stop_event.set()

                # Stop both tasks cleanly.
                for task in (mic_task, speaker_task):
                    if not task.done():
                        task.cancel()

                await asyncio.gather(
                    mic_task,
                    speaker_task,
                    return_exceptions=True,
                )

    except ConnectionClosed as e:
        print(f"\nWebSocket closed: {e}")

    except asyncio.CancelledError:
        raise

    except Exception as e:
        print(f"\nClient error: {type(e).__name__}: {e}")


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nStopped.")