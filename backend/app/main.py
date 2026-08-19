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

    print(
        "EVENT",
        json.dumps(record, ensure_ascii=False),
        flush=True,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    session_id = str(uuid.uuid4())

    log_event(
        session_id,
        "session_started",
    )

    client = genai.Client(
        api_key=os.environ["GEMINI_API_KEY"]
    )

    # Session-level counters
    audio_in_bytes = 0
    audio_out_bytes = 0

    # Current conversational turn
    turn_id = None
    turn_started_at = None

    user_text = ""
    assistant_text = ""

    turn_audio_in_bytes = 0
    turn_audio_out_bytes = 0

    turn_generation_complete = False
    turn_complete = False
    turn_interrupted = False

    def start_turn():
        nonlocal turn_id
        nonlocal turn_started_at
        nonlocal user_text
        nonlocal assistant_text
        nonlocal turn_audio_in_bytes
        nonlocal turn_audio_out_bytes
        nonlocal turn_generation_complete
        nonlocal turn_complete
        nonlocal turn_interrupted

        if turn_id is None:
            turn_id = str(uuid.uuid4())
            turn_started_at = now()

            user_text = ""
            assistant_text = ""

            turn_audio_in_bytes = 0
            turn_audio_out_bytes = 0

            turn_generation_complete = False
            turn_complete = False
            turn_interrupted = False

            log_event(
                session_id,
                "turn_started",
                turn_id=turn_id,
            )

    def finish_turn(reason):
        nonlocal turn_id
        nonlocal turn_started_at
        nonlocal user_text
        nonlocal assistant_text
        nonlocal turn_audio_in_bytes
        nonlocal turn_audio_out_bytes
        nonlocal turn_generation_complete
        nonlocal turn_complete
        nonlocal turn_interrupted

        if turn_id is None:
            return

        log_event(
            session_id,
            "turn_summary",
            turn_id=turn_id,
            started_at=turn_started_at,
            ended_at=now(),
            reason=reason,
            user_text=user_text.strip(),
            assistant_text=assistant_text.strip(),
            audio_in_bytes=turn_audio_in_bytes,
            audio_out_bytes=turn_audio_out_bytes,
            generation_complete=turn_generation_complete,
            turn_complete=turn_complete,
            interrupted=turn_interrupted,
        )

        # Reset turn state
        turn_id = None
        turn_started_at = None

        user_text = ""
        assistant_text = ""

        turn_audio_in_bytes = 0
        turn_audio_out_bytes = 0

        turn_generation_complete = False
        turn_complete = False
        turn_interrupted = False

    try:

        async with client.aio.live.connect(
            model=MODEL,
            config=types.LiveConnectConfig(
                response_modalities=["AUDIO"],

                # Pin the assistant's OUTPUT language. Native-audio models
                # cannot be hard-locked via language_code (unsupported), and
                # Gemini's automatic speech-language detection is unreliable
                # (it sometimes mishears English as Hindi/Malayalam/etc and
                # answers back in that language). A clear system instruction
                # steers the response to always be English regardless of what
                # language the input is transcribed/interpreted as.
                system_instruction=(
                    "You are Sirious, a helpful and concise voice assistant. "
                    "ALWAYS respond in English, no matter what language the "
                    "user speaks or is detected as speaking."
                ),

                input_audio_transcription=(
                    types.AudioTranscriptionConfig()
                ),

                output_audio_transcription=(
                    types.AudioTranscriptionConfig()
                ),
            ),
        ) as session:

            await websocket.send_json({
                "type": "session_started",
                "session_id": session_id,
            })

            async def client_to_gemini():
                nonlocal audio_in_bytes
                nonlocal turn_audio_in_bytes

                try:
                    while True:

                        message = await websocket.receive()

                        if message["type"] == "websocket.disconnect":
                            log_event(
                                session_id,
                                "client_disconnected",
                            )
                            break

                        # -----------------------------
                        # Audio from client
                        # -----------------------------

                        if message.get("bytes"):

                            data = message["bytes"]

                            audio_in_bytes += len(data)

                            # Attribute incoming audio to
                            # the current conversational turn.
                            start_turn()

                            turn_audio_in_bytes += len(data)

                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=data,
                                    mime_type="audio/pcm;rate=16000",
                                )
                            )

                        # -----------------------------
                        # Control messages
                        # -----------------------------

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

                    log_event(
                        session_id,
                        "client_disconnected",
                    )

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
                nonlocal turn_audio_out_bytes
                nonlocal user_text
                nonlocal assistant_text
                nonlocal turn_generation_complete
                nonlocal turn_complete
                nonlocal turn_interrupted
                try:

                    while True:

                        async for response in session.receive():

                            # -----------------------------
                            # Session lifecycle
                            # -----------------------------

                            if response.go_away is not None:

                                time_left = (
                                    response.go_away.time_left
                                )

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

                            # -----------------------------
                            # Session resumption
                            # -----------------------------

                            if response.session_resumption_update:

                                update = (
                                    response.session_resumption_update
                                )

                                if (
                                    update.resumable
                                    and update.new_handle
                                ):

                                    log_event(
                                        session_id,
                                        "session_resumption",
                                        handle_received=True,
                                    )

                                    await websocket.send_json({
                                        "type": "session_resumption",
                                        "handle": (
                                            update.new_handle
                                        ),
                                    })

                            content = response.server_content

                            if not content:
                                continue

                            # -----------------------------
                            # User transcription
                            # -----------------------------

                            if content.input_transcription:

                                text = (
                                    content.input_transcription.text
                                )

                                if text:

                                    start_turn()

                                    user_text += text

                                    log_event(
                                        session_id,
                                        "user_transcript_fragment",
                                        turn_id=turn_id,
                                        text=text,
                                    )

                                    await websocket.send_json({
                                        "type": "user_transcript",
                                        "text": text,
                                    })

                            # -----------------------------
                            # Assistant transcription
                            # -----------------------------

                            if content.output_transcription:

                                text = (
                                    content.output_transcription.text
                                )

                                if text:

                                    start_turn()

                                    assistant_text += text

                                    log_event(
                                        session_id,
                                        "assistant_transcript_fragment",
                                        turn_id=turn_id,
                                        text=text,
                                    )

                                    await websocket.send_json({
                                        "type": "assistant_transcript",
                                        "text": text,
                                    })

                            # -----------------------------
                            # Model audio
                            # -----------------------------

                            if content.model_turn:

                                for part in (
                                    content.model_turn.parts
                                ):

                                    if part.inline_data:

                                        data = (
                                            part.inline_data.data
                                        )

                                        audio_out_bytes += len(data)

                                        turn_audio_out_bytes += (
                                            len(data)
                                        )

                                        await websocket.send_bytes(
                                            data
                                        )

                            # -----------------------------
                            # Generation complete
                            # -----------------------------

                            if content.generation_complete:

                                turn_generation_complete = True

                                log_event(
                                    session_id,
                                    "generation_complete",
                                    turn_id=turn_id,
                                )

                                await websocket.send_json({
                                    "type": "response_finished"
                                })

                            # -----------------------------
                            # User interrupted model
                            # -----------------------------

                            if content.interrupted:

                                turn_interrupted = True

                                log_event(
                                    session_id,
                                    "interrupted",
                                    turn_id=turn_id,
                                )

                                await websocket.send_json({
                                    "type": "interrupted"
                                })

                                finish_turn(
                                    "interrupted"
                                )

                            # -----------------------------
                            # Conversational turn complete
                            # -----------------------------

                            if content.turn_complete:

                                turn_complete = True

                                log_event(
                                    session_id,
                                    "turn_complete",
                                    turn_id=turn_id,
                                )

                                await websocket.send_json({
                                    "type": "turn_complete"
                                })

                                finish_turn(
                                    "turn_complete"
                                )

                except asyncio.CancelledError:
                    raise

                except WebSocketDisconnect:

                    # This is a client lifecycle event,
                    # not a Gemini API error.
                    log_event(
                        session_id,
                        "client_disconnected_while_receiving",
                    )

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

            # -----------------------------------------
            # Run both directions concurrently
            # -----------------------------------------

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

                for task in (
                    client_task,
                    gemini_task,
                ):

                    if not task.done():
                        task.cancel()

                await asyncio.gather(
                    client_task,
                    gemini_task,
                    return_exceptions=True,
                )

    except WebSocketDisconnect:

        log_event(
            session_id,
            "websocket_disconnected",
        )

    except Exception as e:

        log_event(
            session_id,
            "session_error",
            error=repr(e),
        )

    finally:

        # If the client/session disappears before Gemini
        # sends turn_complete, don't lose the accumulated turn.
        if turn_id is not None:

            finish_turn(
                "session_ended"
            )

        log_event(
            session_id,
            "session_ended",
            audio_in_bytes=audio_in_bytes,
            audio_out_bytes=audio_out_bytes,
        )