# import asyncio
# import websockets

# async def main():
#     uri = "wss://sirious-api-635321277027.asia-south1.run.app/ws"

#     async with websockets.connect(uri) as ws:
#         await ws.send("Hello Sirious")
#         response = await ws.recv()
#         print(response)

# asyncio.run(main())

# import asyncio
# import sounddevice as sd
# import websockets

# async def main():
#     uri = "wss://sirious-api-635321277027.asia-south1.run.app/ws"

#     audio = sd.rec(
#         int(3 * 16000),
#         samplerate=16000,
#         channels=1,
#         dtype="int16",
#     )
#     sd.wait()

#     async with websockets.connect(uri) as ws:
#         print("Connected. Sending audio...")
#         await ws.send(audio.tobytes())
#         await ws.send("turn_complete")

#         try:
#             while True:
#                 response = await asyncio.wait_for(ws.recv(), timeout=10)
#                 print(
#                     f"Received from FastAPI: "
#                     f"{len(response)} bytes"
#                 )
#         except asyncio.TimeoutError:
#             print("No response within 10 seconds.")

# asyncio.run(main())


import asyncio
import wave

import numpy as np
import sounddevice as sd
import websockets


SAMPLE_RATE = 16000
RECORD_SECONDS = 3
OUTPUT_FILE = "gemini_cloud_response.wav"


async def main():
    print("Recording...")
    audio = sd.rec(
        int(RECORD_SECONDS * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
    )
    sd.wait()

    uri = "wss://sirious-api-635321277027.asia-south1.run.app/ws"

    response_audio = bytearray()

    async with websockets.connect(uri) as ws:
        print("Connected. Sending audio...")

        await ws.send(audio.tobytes())
        await ws.send("turn_complete")

        try:
            while True:
                data = await asyncio.wait_for(ws.recv(), timeout=30)

                if isinstance(data, bytes):
                    response_audio.extend(data)
                    print(f"Received: {len(data)} bytes")

        except asyncio.TimeoutError:
            print("No data received for 30 seconds.")

    print(f"Total audio received: {len(response_audio)} bytes")

    with wave.open(OUTPUT_FILE, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(response_audio)

    print(f"Saved: {OUTPUT_FILE}")


asyncio.run(main())