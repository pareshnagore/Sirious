import os
import asyncio
import contextlib

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types


app = FastAPI()

MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    try:
        async with client.aio.live.connect(
            model=MODEL,
            config=types.LiveConnectConfig(
                response_modalities=["AUDIO"],
            ),
        ) as session:

            await websocket.send_json({
                "type": "session_started"
            })

            async def client_to_gemini():
                try:
                    while True:
                        message = await websocket.receive()

                        if message["type"] == "websocket.disconnect":
                            print("Client disconnected")
                            break

                        if message.get("bytes"):
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=message["bytes"],
                                    mime_type="audio/pcm;rate=16000",
                                )
                            )

                        elif message.get("text"):
                            control = message["text"]
                            if control == "stop":
                                break
                            elif control == "ping":
                                await websocket.send_json({
                                    "type": "pong"
                                })

                except WebSocketDisconnect:
                    print("Client disconnected")

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    print("CLIENT ERROR:", repr(e))

                    with contextlib.suppress(Exception):
                        await websocket.send_json({
                            "type": "error",
                            "code": "CLIENT_ERROR",
                            "message": str(e),
                        })

                finally:
                    print("Client → Gemini task ended")


            async def gemini_to_client():
                try:
                    while True:
                        async for response in session.receive():

                            # Session lifecycle warning
                            if response.go_away is not None:
                                time_left = response.go_away.time_left

                                print(
                                    "Gemini GoAway:",
                                    time_left,
                                )

                                await websocket.send_json({
                                    "type": "session_warning",
                                    "code": "GO_AWAY",
                                    "time_left": str(time_left),
                                })

                            # Session resumption information
                            if response.session_resumption_update:
                                update = response.session_resumption_update

                                if update.resumable and update.new_handle:
                                    print(
                                        "Received session resumption handle"
                                    )

                                    # We will persist this later.
                                    await websocket.send_json({
                                        "type": "session_resumption",
                                        "handle": update.new_handle,
                                    })

                            content = response.server_content

                            if content:

                                # User transcription
                                if content.input_transcription:
                                    text = content.input_transcription.text

                                    if text:
                                        await websocket.send_json({
                                            "type": "user_transcript",
                                            "text": text,
                                        })

                                # Model transcription
                                if content.output_transcription:
                                    text = content.output_transcription.text

                                    if text:
                                        await websocket.send_json({
                                            "type": "assistant_transcript",
                                            "text": text,
                                        })

                                # Model audio
                                if content.model_turn:
                                    for part in content.model_turn.parts:

                                        if part.inline_data:
                                            await websocket.send_bytes(
                                                part.inline_data.data
                                            )

                                # Generation completed
                                if content.generation_complete:
                                    await websocket.send_json({
                                        "type": "response_finished"
                                    })

                                # Complete conversational turn
                                if content.turn_complete:
                                    await websocket.send_json({
                                        "type": "turn_complete"
                                    })

                                # User interrupted the model
                                if content.interrupted:
                                    await websocket.send_json({
                                        "type": "interrupted"
                                    })

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    print("GEMINI ERROR:", repr(e))

                    with contextlib.suppress(Exception):
                        await websocket.send_json({
                            "type": "error",
                            "code": "GEMINI_ERROR",
                            "message": str(e),
                        })

                finally:
                    print("Gemini → Client task ended")


            client_task = asyncio.create_task(
                client_to_gemini()
            )

            gemini_task = asyncio.create_task(
                gemini_to_client()
            )

            try:
                await asyncio.gather(
                    client_task,
                    gemini_task,
                )

            finally:
                for task in (client_task, gemini_task):
                    if not task.done():
                        task.cancel()

                await asyncio.gather(
                    client_task,
                    gemini_task,
                    return_exceptions=True,
                )

    except WebSocketDisconnect:
        print("WebSocket disconnected")

    except Exception as e:
        print("SESSION ERROR:", repr(e))

    finally:
        print("WebSocket session ended")