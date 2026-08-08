import asyncio
import os
import wave

import numpy as np
import sounddevice as sd
from google import genai


SAMPLE_RATE = 16000
CHANNELS = 1
RECORD_SECONDS = 5
OUTPUT_FILE = "gemini_response.wav"


async def main():
    print("Recording...")
    audio = sd.rec(
        int(RECORD_SECONDS * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
    )
    sd.wait()
    print("Recording complete.")

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    response_audio = bytearray()

    async with client.aio.live.connect(
        model="gemini-2.5-flash-native-audio-preview-12-2025",
        config={"response_modalities": ["AUDIO"]},
    ) as session:

        await session.send_realtime_input(
            audio={
                "data": audio.tobytes(),
                "mime_type": "audio/pcm;rate=16000",
            }
        )

        await session.send_client_content(
            turns={
                "role": "user",
                "parts": [{"text": "Please answer what I just said."}],
            },
            turn_complete=True,
        )

        async for response in session.receive():
            if response.server_content and response.server_content.model_turn:
                for part in response.server_content.model_turn.parts:
                    if part.inline_data:
                        response_audio.extend(part.inline_data.data)

            if response.server_content and response.server_content.turn_complete:
                break

    print(f"Received {len(response_audio)} bytes of audio.")

    with wave.open(OUTPUT_FILE, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(response_audio)

    print(f"Saved response to {OUTPUT_FILE}")


asyncio.run(main())