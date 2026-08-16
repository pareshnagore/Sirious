import asyncio
import json

import sounddevice as sd
import websockets
from websockets.exceptions import ConnectionClosed


URI = "wss://sirious-api-635321277027.asia-south1.run.app/ws"

# Gemini input
SAMPLE_RATE = 16000
CHANNELS = 1

# 100 ms chunks for now.
# We can reduce this to 20-40 ms later for lower latency.
CHUNK_MS = 100
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000

# Gemini output
OUTPUT_SAMPLE_RATE = 24000
OUTPUT_CHANNELS = 1
OUTPUT_BLOCKSIZE = 1920


async def microphone(ws, stop_event):
    """
    Continuously capture microphone audio and send it to the server.
    """
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def callback(indata, frames, time, status):
        if status:
            print("Audio:", status)

        if stop_event.is_set():
            return

        data = indata.copy().tobytes()

        try:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                data,
            )
        except RuntimeError:
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
                data = await asyncio.wait_for(
                    queue.get(),
                    timeout=0.5,
                )
            except asyncio.TimeoutError:
                continue

            if stop_event.is_set():
                break

            try:
                await ws.send(data)
            except ConnectionClosed:
                break


async def receiver(ws, audio_queue, stop_event):
    """
    Receive everything from the server.

    Binary messages = model audio.
    JSON messages  = control/transcription/lifecycle events.
    """

    try:
        while not stop_event.is_set():

            message = await ws.recv()

            # -----------------------------------------
            # Model audio
            # -----------------------------------------

            if isinstance(message, bytes):

                if message:
                    await audio_queue.put(message)

                continue

            # -----------------------------------------
            # Server JSON message
            # -----------------------------------------

            try:
                event = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                continue

            event_type = event.get("type")

            # -----------------------------------------
            # IMPORTANT:
            # Gemini interrupted the current response.
            # -----------------------------------------

            if event_type == "interrupted":

                print("\n[interrupted]")

                # Discard all model audio that has not
                # yet been played.
                while True:
                    try:
                        audio_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                # Tell speaker to flush whatever it can.
                await audio_queue.put(None)

            elif event_type == "user_transcript":

                text = event.get("text", "")

                if text:
                    print(f"\nUSER: {text}", end="", flush=True)

            elif event_type == "assistant_transcript":

                text = event.get("text", "")

                if text:
                    print(
                        f"\nASSISTANT: {text}",
                        end="",
                        flush=True,
                    )

            elif event_type == "response_finished":

                print("\n[response generation finished]")

            elif event_type == "turn_complete":

                print("\n[turn complete]")

            elif event_type == "session_warning":

                print(
                    f"\n[session warning] "
                    f"{event.get('time_left')}"
                )

            elif event_type == "error":

                print(
                    f"\n[server error] "
                    f"{event.get('message')}"
                )

    except ConnectionClosed:
        pass

    finally:
        stop_event.set()

        # Wake the speaker if it is waiting.
        try:
            await audio_queue.put(None)
        except Exception:
            pass


async def speaker(audio_queue, stop_event):
    """
    Play model audio.

    None is a control marker meaning:
    flush/reset playback because the response was interrupted.
    """

    stream = None

    def open_stream():
        new_stream = sd.RawOutputStream(
            samplerate=OUTPUT_SAMPLE_RATE,
            channels=OUTPUT_CHANNELS,
            dtype="int16",
            blocksize=OUTPUT_BLOCKSIZE,
        )

        new_stream.start()

        return new_stream

    try:
        stream = open_stream()

        while not stop_event.is_set():

            data = await audio_queue.get()

            if data is None:

                # -------------------------------------
                # INTERRUPTION / FLUSH
                # -------------------------------------

                if stream is not None:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass

                    stream = None

                # Re-open an empty playback stream.
                if not stop_event.is_set():
                    stream = open_stream()

                continue

            if stream is not None and data:
                try:
                    stream.write(data)
                except Exception:
                    break

    finally:

        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass

            try:
                stream.close()
            except Exception:
                pass


async def main():

    print("Connecting...")

    stop_event = asyncio.Event()

    # Audio generated by Gemini waits here before
    # being consumed by the speaker.
    audio_queue = asyncio.Queue()

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
                microphone(
                    ws,
                    stop_event,
                )
            )

            receiver_task = asyncio.create_task(
                receiver(
                    ws,
                    audio_queue,
                    stop_event,
                )
            )

            speaker_task = asyncio.create_task(
                speaker(
                    audio_queue,
                    stop_event,
                )
            )

            try:

                await asyncio.gather(
                    mic_task,
                    receiver_task,
                    speaker_task,
                )

            finally:

                stop_event.set()

                for task in (
                    mic_task,
                    receiver_task,
                    speaker_task,
                ):
                    if not task.done():
                        task.cancel()

                await asyncio.gather(
                    mic_task,
                    receiver_task,
                    speaker_task,
                    return_exceptions=True,
                )

    except ConnectionClosed as e:

        print(
            f"\nWebSocket closed: {e}"
        )

    except asyncio.CancelledError:

        raise

    except Exception as e:

        print(
            f"\nClient error: "
            f"{type(e).__name__}: {e}"
        )


try:

    asyncio.run(main())

except KeyboardInterrupt:

    print("\nStopped.")