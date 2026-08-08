import os
import asyncio

from fastapi import FastAPI, WebSocket
from google import genai

app = FastAPI()

@app.get("/health")
async def health():
    return {"status": "ok"}


# @app.websocket("/ws")
# async def websocket_endpoint(websocket: WebSocket):
#     await websocket.accept()

#     client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

#     async with client.aio.live.connect(
#         model="gemini-2.5-flash-native-audio-preview-12-2025",
#         config={"response_modalities": ["AUDIO"]},
#     ) as session:

#         while True:
#             message = await websocket.receive()

#             if message.get("text"):
#                 print("Control:", message["text"])

#                 if message["text"] == "stop":
#                     break

#             elif message.get("bytes"):
#                 await session.send_realtime_input(
#                     audio={
#                         "data": message["bytes"],
#                         "mime_type": "audio/pcm;rate=16000",
#                     }
#                 )

#                 async for response in session.receive():
#                     if (
#                         response.server_content
#                         and response.server_content.model_turn
#                     ):
#                         for part in response.server_content.model_turn.parts:
#                             if part.inline_data:
#                                 await websocket.send_bytes(
#                                     part.inline_data.data
#                                 )

#     await websocket.close()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    async with client.aio.live.connect(
        model="gemini-2.5-flash-native-audio-preview-12-2025",
        config={"response_modalities": ["AUDIO"]},
    ) as session:

        async def client_to_gemini():
            while True:
                message = await websocket.receive()

                if message.get("bytes"):
                    await session.send_realtime_input(
                        audio={
                            "data": message["bytes"],
                            "mime_type": "audio/pcm;rate=16000",
                        }
                    )

                elif message.get("text"):
                    if message["text"] == "turn_complete":
                        await session.send_realtime_input(
                            audio_stream_end=True
                        )
                        await session.send_client_content(
                            turns={
                                "role": "user",
                                "parts": [
                                    {"text": "Please respond to what I just said."}
                                ],
                            },
                            turn_complete=True,
                        )
                    elif message["text"] == "stop":
                        break

        async def gemini_to_client():
            async for response in session.receive():
                if (
                    response.server_content
                    and response.server_content.model_turn
                ):
                    for part in response.server_content.model_turn.parts:
                        if part.inline_data:
                            await websocket.send_bytes(
                                part.inline_data.data
                            )

        client_task = asyncio.create_task(client_to_gemini())
        gemini_task = asyncio.create_task(gemini_to_client())

        done, pending = await asyncio.wait(
            [client_task, gemini_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()