import os
import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types


app = FastAPI()

MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"


def now():
    return datetime.now(timezone.utc).isoformat()


def log_event(session_id, event, **data):
    record = {
        "timestamp": now(),
        "session_id": session_id,
        "event": event,
        **data,
    }
    print("EVENT", json.dumps(record, ensure_ascii=False), flush=True)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    session_id = str(uuid.uuid4())

    log_event(session_id, "session_started")

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    audio_in_bytes = 0
    audio_out_bytes = 0

    try:
        async with client.aio.live.connect(
            model=MODEL,
            config=types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                input_audio_transcription=types.AudioTranscriptionConfig(),
                output_audio_transcription=types.AudioTranscriptionConfig(),
            ),
        ) as session:

            await websocket.send_json({
                "type": "session_started",
                "session_id": session_id,
            })

            async def client_to_gemini():
                nonlocal audio_in_bytes

                try:
                    while True:
                        message = await websocket.receive()

                        if message["type"] == "websocket.disconnect":
                            log_event(session_id, "client_disconnected")
                            break

                        if message.get("bytes"):
                            data = message["bytes"]
                            audio_in_bytes += len(data)

                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=data,
                                    mime_type="audio/pcm;rate=16000",
                                )
                            )

                        elif message.get("text"):
                            control = message["text"]

                            log_event(
                                session_id,
                                "control",
                                value=control,
                            )

                            if control == "stop":
                                break

                            elif control == "ping":
                                await websocket.send_json({
                                    "type": "pong"
                                })

                except WebSocketDisconnect:
                    log_event(session_id, "client_disconnected")

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    log_event(
                        session_id,
                        "client_error",
                        error=repr(e),
                    )

                    with contextlib.suppress(Exception):
                        await websocket.send_json({
                            "type": "error",
                            "code": "CLIENT_ERROR",
                            "message": str(e),
                        })

                finally:
                    log_event(
                        session_id,
                        "audio_in_summary",
                        bytes=audio_in_bytes,
                    )

            async def gemini_to_client():
                nonlocal audio_out_bytes

                try:
                    while True:
                        async for response in session.receive():

                            # Session lifecycle warning
                            if response.go_away is not None:
                                time_left = response.go_away.time_left

                                log_event(
                                    session_id,
                                    "go_away",
                                    time_left=str(time_left),
                                )

                                await websocket.send_json({
                                    "type": "session_warning",
                                    "code": "GO_AWAY",
                                    "time_left": str(time_left),
                                })

                            # Session resumption
                            if response.session_resumption_update:
                                update = response.session_resumption_update

                                if update.resumable and update.new_handle:
                                    log_event(
                                        session_id,
                                        "session_resumption",
                                        handle_received=True,
                                    )

                                    await websocket.send_json({
                                        "type": "session_resumption",
                                        "handle": update.new_handle,
                                    })

                            content = response.server_content

                            if not content:
                                continue

                            # User transcription
                            if content.input_transcription:
                                text = content.input_transcription.text

                                if text:
                                    log_event(
                                        session_id,
                                        "user_transcript",
                                        text=text,
                                    )

                                    await websocket.send_json({
                                        "type": "user_transcript",
                                        "text": text,
                                    })

                            # Model transcription
                            if content.output_transcription:
                                text = content.output_transcription.text

                                if text:
                                    log_event(
                                        session_id,
                                        "assistant_transcript",
                                        text=text,
                                    )

                                    await websocket.send_json({
                                        "type": "assistant_transcript",
                                        "text": text,
                                    })

                            # Model audio
                            if content.model_turn:
                                for part in content.model_turn.parts:

                                    if part.inline_data:
                                        data = part.inline_data.data
                                        audio_out_bytes += len(data)

                                        await websocket.send_bytes(data)

                            # Generation completed
                            if content.generation_complete:
                                log_event(
                                    session_id,
                                    "generation_complete",
                                )

                                await websocket.send_json({
                                    "type": "response_finished"
                                })

                            # Conversational turn completed
                            if content.turn_complete:
                                log_event(
                                    session_id,
                                    "turn_complete",
                                    audio_in_bytes=audio_in_bytes,
                                    audio_out_bytes=audio_out_bytes,
                                )

                                await websocket.send_json({
                                    "type": "turn_complete"
                                })

                            # User interrupted model
                            if content.interrupted:
                                log_event(
                                    session_id,
                                    "interrupted",
                                )

                                await websocket.send_json({
                                    "type": "interrupted"
                                })

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    log_event(
                        session_id,
                        "gemini_error",
                        error=repr(e),
                    )

                    with contextlib.suppress(Exception):
                        await websocket.send_json({
                            "type": "error",
                            "code": "GEMINI_ERROR",
                            "message": str(e),
                        })

                finally:
                    log_event(
                        session_id,
                        "audio_out_summary",
                        bytes=audio_out_bytes,
                    )

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
        log_event(session_id, "websocket_disconnected")

    except Exception as e:
        log_event(
            session_id,
            "session_error",
            error=repr(e),
        )

    finally:
        log_event(
            session_id,
            "session_ended",
            audio_in_bytes=audio_in_bytes,
            audio_out_bytes=audio_out_bytes,
        )