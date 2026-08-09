import os
import asyncio

from fastapi import FastAPI, WebSocket
from google import genai

app = FastAPI()

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    async with client.aio.live.connect(
        model="gemini-2.5-flash-native-audio-preview-12-2025",
        config={"response_modalities": ["AUDIO"]},
    ) as session:

        async def client_to_gemini():
            try:
                while True:
                    message = await websocket.receive()

                    if message.get("bytes"):
                        print("Received audio:", len(message["bytes"]))
                        await session.send_realtime_input(
                            audio={
                                "data": message["bytes"],
                                "mime_type": "audio/pcm;rate=16000",
                            }
                        )
                    elif message.get("text"):
                        print("Control:", message["text"])
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
            except Exception as e:
                print("CLIENT ERROR:", repr(e))
                raise
            finally:
                print("Client send task ended")


        async def gemini_to_client():
            try:
                while True:
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

                        if (
                            response.server_content
                            and response.server_content.turn_complete
                        ):
                            print("Gemini turn complete")

                            # session.receive() will finish this turn.
                            # Outer while loop waits for the next turn.
            except Exception as e:
                print("GEMINI ERROR:", repr(e))
                raise
            finally:
                print("Gemini receive task ended")


        client_task = asyncio.create_task(client_to_gemini())
        gemini_task = asyncio.create_task(gemini_to_client())

        try:
            await asyncio.gather(
                client_task,
                gemini_task,
            )
        finally:
            client_task.cancel()
            gemini_task.cancel()