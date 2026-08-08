# import asyncio
# import os
# from google import genai

# async def main():
#     client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

#     async with client.aio.live.connect(
#         model="gemini-2.5-flash-native-audio-preview-12-2025",
#         config={"response_modalities": ["TEXT"]},
#     ) as session:
#         await session.send_client_content(
#             turns={
#                 "role": "user",
#                 "parts": [{"text": "Hello Sirious. Say hello back."}],
#             },
#             turn_complete=True,
#         )

#         async for response in session.receive():
#             if response.text:
#                 print(response.text)

# asyncio.run(main())


import asyncio
import os
from google import genai


async def main():
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    async with client.aio.live.connect(
        model="gemini-2.5-flash-native-audio-preview-12-2025",
        config={
            "response_modalities": ["AUDIO"],
            "output_audio_transcription": {},
        },
    ) as session:

        await session.send_client_content(
            turns={
                "role": "user",
                "parts": [{"text": "Hello Sirious. Say hello back."}],
            },
            turn_complete=True,
        )

        async for response in session.receive():
            if response.text:
                # print("Sirious:", response.text)
                print(response)


asyncio.run(main())