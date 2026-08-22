import os
import asyncio
import contextlib
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi import Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types

from .store import get_store


app = FastAPI()

# Model is env-configurable so Cloud Run can switch without a code change.
# NOTE (verified 21 Aug by direct probe): gemini-2.5-flash-native-audio-* NEVER
# emits a RESUMABLE session_resumption handle → session resumption stays
# dormant on it. gemini-3.1-flash-live-preview DOES (handle arrives right
# after the first turn completes). Set SIRIOUS_MODEL to enable resumption.
MODEL = os.environ.get(
    "SIRIOUS_MODEL",
    "gemini-2.5-flash-native-audio-preview-12-2025",
)

# ── Lightweight auth (Phase 2) ──────────────────────────────────────────────
# Static bearer token. When SIRIOUS_AUTH_TOKEN is set, ALL endpoints — the
# REST history API AND the /ws handshake — require it. Unset → open access
# (local dev only; Cloud Run always sets it).
AUTH_TOKEN = os.environ.get("SIRIOUS_AUTH_TOKEN")


def _check_auth(authorization: str | None) -> None:
    if not AUTH_TOKEN:
        return
    if authorization != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_auth(
    authorization: str | None = Header(default=None),
) -> None:
    _check_auth(authorization)


def check_ws_auth(websocket: WebSocket, client_session_id: str | None) -> bool:
    """Handshake auth: token must arrive as ?token=... (browsers/WS clients
    can't set custom headers universally). Reject with 401 before accept()."""
    if not AUTH_TOKEN:
        return True
    supplied = websocket.query_params.get("token")
    if supplied != AUTH_TOKEN:
        log_event(
            str(uuid.uuid4()),
            "ws_auth_rejected",
        )
        return False
    return True

# ── Session resumption (protocol v2) ────────────────────────────────────────
# Maps a client-supplied stable session id → the latest Gemini resumption
# handle, so a reconnecting client can resume the SAME Gemini session (model
# memory intact) instead of starting fresh. Handles are valid for 2 h after
# the session ends (Gemini docs: ai.google.dev/gemini-api/docs/live-session).
# In-memory by design: single-instance Cloud Run service. A recycled instance
# simply loses resumability — clients fall back to a fresh session.
RESUME_HANDLE_TTL_S = 2 * 60 * 60  # 2 hours

_resume_handles: dict[str, dict] = {}


def _store_handle(client_session_id: str, handle: str) -> None:
    _resume_handles[client_session_id] = {
        "handle": handle,
        "updated_at": time.monotonic(),
    }


def _pop_handle(client_session_id: str):
    """Return a still-valid handle for this id (and drop expired entries)."""
    entry = _resume_handles.get(client_session_id)
    if entry is None:
        return None
    if time.monotonic() - entry["updated_at"] > RESUME_HANDLE_TTL_S:
        del _resume_handles[client_session_id]
        return None
    return entry["handle"]


def _drop_handle(client_session_id: str) -> None:
    _resume_handles.pop(client_session_id, None)


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


# ── Session history API (Phase 2) ───────────────────────────────────────────

@app.get("/sessions")
async def list_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    _: None = Depends(require_auth),
):
    try:
        items = await get_store().list_sessions(limit=limit)
    except Exception as e:  # noqa: BLE001 — report, don't crash
        log_event("rest", "sessions_list_error", error=repr(e))
        return JSONResponse(status_code=503, content={"detail": "store unavailable"})
    return {"sessions": items}


@app.get("/sessions/{doc_id}")
async def get_session(
    doc_id: str,
    _: None = Depends(require_auth),
):
    try:
        doc = await get_store().get_session(doc_id)
    except Exception as e:  # noqa: BLE001 — report, don't crash
        log_event("rest", "session_get_error", error=repr(e), doc_id=doc_id)
        return JSONResponse(status_code=503, content={"detail": "store unavailable"})
    if doc is None:
        raise HTTPException(status_code=404, detail="session not found")
    turns = doc.get("turns") or []
    return {
        "id": doc_id,
        **{k: doc.get(k) for k in (
            "client_session_id", "title", "model", "device", "started_at",
            "ended_at", "duration_s", "end_reason", "resume_count",
            "turn_count",
        )},
        "turns": [
            {
                "id": t.get("id"),
                "started_at": t.get("started_at"),
                "ended_at": t.get("ended_at"),
                "user_text": t.get("user_text") or "",
                "assistant_text": t.get("assistant_text") or "",
                "interrupted": bool(t.get("interrupted")),
                "reason": t.get("reason"),
            }
            for t in turns
        ],
    }


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    client_session_id: str | None = None,
):
    # Handshake auth (Phase 2): reject BEFORE accept so no Gemini session
    # is ever opened for an unauthenticated peer.
    if not check_ws_auth(websocket, client_session_id):
        await websocket.close(code=4401, reason="unauthorized")
        return

    await websocket.accept()

    session_id = str(uuid.uuid4())

    # ── Session resumption ──────────────────────────────────────────────────
    # A client that wants continuity across reconnects sends a stable
    # client_session_id. If we hold a live resumption handle for it, resume
    # the SAME Gemini session; otherwise start fresh.
    resumed = False
    resume_handle = _pop_handle(client_session_id) if client_session_id else None
    if resume_handle is not None:
        resumed = True

    # Persistence (Phase 2): one Firestore doc per LOGICAL conversation —
    # doc id == client_session_id, so a resuming reconnect EXTENDS the same
    # document rather than creating a new one.
    store = get_store()
    doc_id = client_session_id or session_id
    store.start_session(
        doc_id,
        client_session_id=client_session_id,
        model=MODEL,
        resumed=resumed,
        device=websocket.headers.get("user-agent"),
        now_iso=now(),
    )

    log_event(
        session_id,
        "session_started",
        client_session_id=client_session_id,
        resumed=resumed,
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

        # Persist this turn (Phase 2). Non-blocking enqueue; the store's
        # writer task owns the actual Firestore write.
        store.record_turn(
            doc_id,
            summary={
                "turn_id": turn_id,
                "started_at": turn_started_at,
                "ended_at": now(),
                "reason": reason,
                "user_text": user_text.strip(),
                "assistant_text": assistant_text.strip(),
                "audio_in_bytes": turn_audio_in_bytes,
                "audio_out_bytes": turn_audio_out_bytes,
                "generation_complete": turn_generation_complete,
                "turn_complete": turn_complete,
                "interrupted": turn_interrupted,
            },
            now_iso=now(),
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

                # Resume the previous Gemini session when the client
                # reconnects with a known client_session_id (handle=None
                # simply starts a fresh session).
                session_resumption=types.SessionResumptionConfig(
                    handle=resume_handle,
                ),

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
                "resumed": resumed,
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
                                # Clean end — the conversation is over. Drop
                                # the resumption handle so a future session
                                # with this client_session_id starts fresh.
                                if client_session_id:
                                    _drop_handle(client_session_id)
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

                                    # Persist for a future reconnect from the
                                    # same client_session_id (no-op when the
                                    # client didn't supply one).
                                    if client_session_id:
                                        _store_handle(
                                            client_session_id,
                                            update.new_handle,
                                        )

                                    log_event(
                                        session_id,
                                        "session_resumption",
                                        handle_received=True,
                                        stored=bool(client_session_id),
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

        # Finalize the Firestore doc (Phase 2). The queued session_end op
        # carries the full buffered turns array, so even if the instance is
        # killed right now every completed turn was already written
        # turn-by-turn above.
        store.end_session(
            doc_id,
            now_iso=now(),
            reason="session_ended",
            audio_in_bytes=audio_in_bytes,
            audio_out_bytes=audio_out_bytes,
        )

        log_event(
            session_id,
            "session_ended",
            audio_in_bytes=audio_in_bytes,
            audio_out_bytes=audio_out_bytes,
        )