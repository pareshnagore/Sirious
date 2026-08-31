import os
import asyncio
import contextlib
import traceback
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi import Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from google import genai
from google.genai import types

from .store import get_store
from .stt import DeepgramAmbient, DiarizedUtterance
from .memory import get_memory_store
from .tools import (
    build_registry,
    get_reminder_store,
    process_fired_reminder,
    verify_tasks_oidc,
    _scheduler_from_env,
)
from .fcm import (
    DeviceTokenStore,
    deliver_reminder_to_all_devices,
)
from .relay import ActivityWindowGuard


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

# Phase 6 step 5: distinctive WS close code for "Gemini leg lost, phone
# should reconnect to resume the SAME conversation". 4xxx codes are app-
# defined (4000-4999 reserved for private use per RFC 6455) — the phone's
# blip reconnect path treats ANY abnormal close as reconnect-worthy, and
# this code makes the cause diagnosable in logs on both sides.
CLOSE_RECOVER = 4402


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
    # Ambient turns (Phase 5) pass through with their own shape; voice turns
    # keep the normalized voice shape.
    # Phase 5 C+B: a doc may be MIXED — ambient room turns (kind
    # "ambient") and voice turns (kind "voice"/absent) in one session
    # document. Shape each turn by ITS OWN kind, not the doc's mode.
    turns_out = []
    for t in turns:
        if t.get("kind") == "ambient":
            turns_out.append({
                "id": t.get("id"),
                "kind": "ambient",
                "ended_at": t.get("ended_at"),
                "speaker": t.get("speaker"),
                "text": t.get("text") or "",
                "start_s": t.get("start_s"),
                "end_s": t.get("end_s"),
            })
        else:
            turns_out.append({
                "id": t.get("id"),
                "kind": "voice",
                "started_at": t.get("started_at"),
                "ended_at": t.get("ended_at"),
                "user_text": t.get("user_text") or "",
                "assistant_text": t.get("assistant_text") or "",
                "interrupted": bool(t.get("interrupted")),
                "reason": t.get("reason"),
            })
    return {
        "id": doc_id,
        **{k: doc.get(k) for k in (
            "client_session_id", "title", "model", "device", "started_at",
            "ended_at", "duration_s", "end_reason", "resume_count",
            "turn_count", "mode",
        )},
        "turns": turns_out,
    }


# ── Memory API (Phase 3) ─────────────────────────────────────────────────────

@app.get("/memories")
async def list_memories(
    q: str | None = Query(default=None, description="Optional semantic query; omit for newest-first"),
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(require_auth),
):
    memory = get_memory_store()
    try:
        if q:
            items = await memory.recall(q, top_k=limit)
            return {"memories": items, "query": q}
        return {"memories": await memory.list_memories(limit=limit)}
    except Exception as e:  # noqa: BLE001
        log_event("rest", "memories_list_error", error=repr(e))
        return JSONResponse(status_code=503, content={"detail": "memory store unavailable"})


@app.delete("/memories/{memory_id}")
async def delete_memory(
    memory_id: str,
    _: None = Depends(require_auth),
):
    memory = get_memory_store()
    try:
        deleted = await memory.soft_delete(memory_id)
    except Exception as e:  # noqa: BLE001
        log_event("rest", "memory_delete_error", error=repr(e), memory_id=memory_id)
        return JSONResponse(status_code=503, content={"detail": "memory store unavailable"})
    if not deleted:
        raise HTTPException(status_code=404, detail="memory not found")
    log_event("rest", "memory_deleted", memory_id=memory_id)
    return {"deleted": True, "id": memory_id}


@app.delete("/sessions/{doc_id}")
async def delete_session(
    doc_id: str,
    _: None = Depends(require_auth),
):
    """Delete a conversation AND its memory footprint: provenance entries
    citing it are stripped; memories left sourceless are removed outright."""
    try:
        deleted = await get_store().delete_session(doc_id)
    except Exception as e:  # noqa: BLE001 — report, don't crash
        log_event("rest", "session_delete_error", error=repr(e), doc_id=doc_id)
        return JSONResponse(status_code=503, content={"detail": "store unavailable"})
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")

    memory = get_memory_store()
    try:
        stats = await memory.strip_provenance(doc_id)
        await memory.delete_session_meta(doc_id)
    except Exception as e:  # noqa: BLE001 — session itself is already gone
        log_event("rest", "memory_cascade_error", error=repr(e), doc_id=doc_id)
        stats = {"memories_updated": 0, "memories_deleted": 0, "cascade_error": True}

    log_event("rest", "session_deleted", doc_id=doc_id, **stats)
    return {"deleted": True, "id": doc_id, "memories": stats}


# ── Reminder firing (Phase 4 chunk 2) ────────────────────────────────────────
# Cloud Tasks one-shot tasks hit this endpoint at each reminder's due instant
# carrying an OIDC ID token minted by SIRIOUS_FIRE_OIDC_SA. Auth is the token,
# not the bearer token: only our scheduler can reach this route.

FIRE_AUDIENCE = os.environ.get("SIRIOUS_FIRE_URL", "")


class _FireRequest(BaseModel):
    reminder_id: str


@app.post("/internal/fire-reminder")
async def fire_reminder(
    request: _FireRequest,
    authorization: str | None = Header(default=None),
    x_goog_iap_jwt_assertion: str | None = Header(default=None),
):
    """Cloud Tasks → here. Verifies the OIDC token (audience must equal
    SIRIOUS_FIRE_URL), then process_fired_reminder does the idempotent
    scheduled→fired flip and dispatches the push. 2xx stops retries.

    NOTE (learned in prod, rev 00033): for plain HTTP targets Cloud Tasks
    delivers the OIDC token in the AUTHORIZATION header ("Bearer <id-token>") —
    the X-Goog-Iap-Jwt-Assertion header is IAP's convention. Both accepted."""
    if not FIRE_AUDIENCE:
        return JSONResponse(status_code=503, content={"detail": "fire path not configured"})
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
    token = token or x_goog_iap_jwt_assertion
    if not token:
        log_event("fire", "fire_token_missing")
        return JSONResponse(status_code=401, content={"detail": "missing task token"})
    email, err = verify_tasks_oidc(
        token,
        FIRE_AUDIENCE,
        expected_signer=os.environ.get("SIRIOUS_FIRE_OIDC_SA") or None,
    )
    if err:
        log_event("fire", "fire_auth_rejected", error=err)
        return JSONResponse(status_code=401, content={"detail": err})
    log_event("fire", "fire_authenticated", signer=email)

    status, body = await process_fired_reminder(
        request.reminder_id,
        get_reminder_store(),
        # Chunk 3: real FCM delivery. Runs after the idempotent flip, so a
        # push failure can never cause a Cloud Tasks retry of a fired
        # reminder; dead tokens are pruned on UNREGISTERED.
        push_send=lambda text, data: deliver_reminder_to_all_devices(
            text,
            data.get("reminder_id", ""),
            get_device_token_store(),
        ),
    )
    log_event(
        "fire",
        "reminder_fired",
        reminder_id=request.reminder_id,
        http=status,
        **{"result": body.get("result", body.get("error", ""))},
    )
    return JSONResponse(status_code=status, content=body)


@app.post("/internal/reminders/selftest")
async def reminders_selftest(_: None = Depends(require_auth)):
    """Chunk-2 prod probe: seeds a scheduled reminder due +2 min and creates
    the real Cloud Tasks task via the same scheduler confirm_reminder uses.
    Bearer-auth like every other REST route. Watch it land on
    /internal/fire-reminder ~2 minutes later."""
    import time as _t

    store = get_reminder_store()
    scheduler = _scheduler_from_env()
    now = _t.time()
    rid = await store.create_draft(
        text="chunk2 prod probe — scheduling works end to end",
        due_ts=now + 120,
        due_iso=datetime.fromtimestamp(now + 120, tz=timezone.utc).isoformat(),
        doc_id="probe-selftest",
        created_at=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    )
    await store.set_status(rid, "scheduled")
    task_name = await scheduler.schedule(rid, now + 120)
    await store.set_task(rid, task_name)
    log_event("fire", "selftest_scheduled", reminder_id=rid, task=task_name or "")
    return {
        "reminder_id": rid,
        "task_scheduled": task_name is not None,
        "task_name": task_name,
        "note": "poll Firestore for status=fired in ~2-3 min",
    }


# ── FCM device registration (chunk 3) ────────────────────────────────────────
# The Android app POSTs its FCM token here after Firebase init at startup.

_device_tokens_singleton: DeviceTokenStore | None = None


def get_device_token_store() -> DeviceTokenStore:
    global _device_tokens_singleton
    if _device_tokens_singleton is None:
        _device_tokens_singleton = DeviceTokenStore()
    return _device_tokens_singleton


class _DeviceRegisterRequest(BaseModel):
    token: str


@app.post("/devices/register")
async def register_device(
    request: _DeviceRegisterRequest,
    _: None = Depends(require_auth),
):
    """Register/refresh this device's FCM token (bearer-auth)."""
    doc_id = await get_device_token_store().register(request.token)
    log_event("fcm", "device_registered", doc=doc_id[:12])
    return {"registered": True, "id": doc_id[:12]}


@app.post("/devices/unregister")
async def unregister_device(
    request: _DeviceRegisterRequest,
    _: None = Depends(require_auth),
):
    """Remove a device's FCM token (bearer-auth; e.g. app logout/wipe)."""
    await get_device_token_store().remove(request.token)
    log_event("fcm", "device_unregistered")
    return {"removed": True}


@app.websocket("/ws/ambient")
async def ambient_ws_endpoint(
    websocket: WebSocket,
    client_session_id: str | None = None,
):
    """Phase 5 C1 — AMBIENT MODE: mic audio -> Deepgram STT+diarization.

    Structural silence: NO Gemini session exists here, nothing can talk.
    Binary frames = 16 kHz PCM16 mono -> provider. JSON in = control
    ("ping"/"stop"). JSON out = {"type":"ambient_segment", speaker, text,
    start_s, end_s} per utterance + session_started/pong/error lifecycle.
    Turns persist via the same queue+writer pattern as voice sessions
    (mode="ambient", kind="ambient" turns with speaker tags).
    """
    if not check_ws_auth(websocket, client_session_id):
        await websocket.close(code=4401, reason="unauthorized")
        return
    if os.environ.get("DEEPGRAM_KEY", "") == "":
        await websocket.close(code=4503, reason="ambient STT not configured")
        return

    await websocket.accept()
    session_id = str(uuid.uuid4())
    doc_id = client_session_id or session_id
    store = get_store()

    store.start_session(
        doc_id,
        client_session_id=client_session_id,
        model="deepgram-nova-3",
        resumed=False,
        device=websocket.headers.get("user-agent"),
        now_iso=now(),
        mode="ambient",
    )
    log_event(session_id, "ambient_session_started", client_session_id=client_session_id)
    await websocket.send_json({
        "type": "session_started",
        "session_id": session_id,
        "mode": "ambient",
    })

    loop = asyncio.get_running_loop()
    audio_in_bytes = 0
    provider: DeepgramAmbient | None = None
    stop_event = asyncio.Event()

    # ── Turn builder (Phase 5): merge same-speaker segments into turns ──
    # Deepgram endpointing splits fluent TTS/continuous speech at every
    # micro-pause (first on-device test: one sentence → 4 turns). A turn is
    # the readable unit, so buffer segments and flush when: speaker changes,
    # gap > AMBIENT_MERGE_GAP_S, idle timer fires, or the session ends.
    AMBIENT_MERGE_GAP_S = 2.0
    pending: dict | None = None  # {speaker, text, start_s, end_s}
    flush_task: asyncio.Task | None = None

    def _flush_payload(p: dict) -> dict:
        return {
            "type": "ambient_segment",
            "speaker": p["speaker"],
            "text": p["text"],
            "start_s": p["start_s"],
            "end_s": p["end_s"],
        }

    def _persist(p: dict) -> None:
        store.record_ambient_turn(
            doc_id,
            speaker_tag=p["speaker"],
            text=p["text"],
            start_s=p["start_s"],
            end_s=p["end_s"],
            now_iso=now(),
        )
        log_event(session_id, "ambient_turn", speaker=p["speaker"], chars=len(p["text"]))
        asyncio.run_coroutine_threadsafe(_safe_send(_flush_payload(p)), loop)

    def _flush_pending() -> None:
        nonlocal pending
        if pending is not None:
            _persist(pending)
            pending = None

    def _on_segment(seg: DiarizedUtterance) -> None:
        """Called from the provider's recv task. Never blocks: persistence
        goes through the store queue; client push is scheduled on the loop."""
        nonlocal pending, flush_task
        nonlocal audio_in_bytes  # noqa: F841 — symmetric with voice path

        if (
            pending is not None
            and pending["speaker"] == seg.speaker_tag
            and seg.start - pending["end_s"] <= AMBIENT_MERGE_GAP_S
        ):
            pending["text"] = f"{pending['text']} {seg.text}".strip()
            pending["end_s"] = seg.end
        else:
            _flush_pending()
            pending = {
                "speaker": seg.speaker_tag,
                "text": seg.text,
                "start_s": seg.start,
                "end_s": seg.end,
            }

        # Idle flush: if no further segment merges within the gap window,
        # emit the pending turn (keeps live UI + Firestore fresh).
        if flush_task is not None:
            flush_task.cancel()
        flush_task = loop.create_task(_idle_flush())

    async def _idle_flush() -> None:
        try:
            await asyncio.sleep(2.5)
            _flush_pending()
        except asyncio.CancelledError:
            pass

    async def _safe_send(payload: dict) -> None:
        with contextlib.suppress(Exception):
            await websocket.send_json(payload)

    # NOTE: DeepgramAmbient callbacks fire on the event loop (its recv task
    # lives in the same loop), so run_coroutine_threadsafe is belt-and-braces
    # for loop affinity — cheap and correct either way.

    provider = DeepgramAmbient(_on_segment)
    try:
        await provider.start()
    except Exception as e:  # noqa: BLE001
        log_event(session_id, "ambient_provider_start_error", error=repr(e))
        await websocket.send_json({"type": "error", "message": "STT unavailable"})
        store.end_session(
            doc_id, now_iso=now(), reason="provider_start_failed",
            audio_in_bytes=0, audio_out_bytes=0,
        )
        await websocket.close(code=1011, reason="stt unavailable")
        return

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                log_event(session_id, "ambient_client_disconnected")
                break
            if message.get("bytes"):
                data = message["bytes"]
                audio_in_bytes += len(data)
                await provider.feed(data)
            elif message.get("text"):
                control = message["text"]
                if control == "stop":
                    break
                if control == "ping":
                    await _safe_send({"type": "pong"})
    except WebSocketDisconnect:
        log_event(session_id, "ambient_client_disconnected")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log_event(session_id, "ambient_session_error", error=repr(e))
    finally:
        stop_event.set()
        _flush_pending()  # emit any pending merged turn before teardown
        if flush_task is not None:
            flush_task.cancel()
        if provider is not None:
            await provider.close()
        store.end_session(
            doc_id,
            now_iso=now(),
            reason="session_ended",
            audio_in_bytes=audio_in_bytes,
            audio_out_bytes=0,
        )
        log_event(session_id, "ambient_session_ended", audio_in_bytes=audio_in_bytes)


def _clean_query_text(raw: str | None, cap: int) -> str:
    """Trim + cap a handshake query param. Blank/whitespace -> empty."""
    if not raw:
        return ""
    return raw.strip()[:cap]


def _ambient_seed_block(seed: str) -> str:
    """System-instruction block for the C2 ambient room-context seed.

    Empty seed -> empty string (no block). The seed arrives from the phone
    as diarized lines ("S1: …\nS2: …") — we treat it as live room context
    the model may REFER to but must not repeat back.
    """
    if not seed.strip():
        return ""
    return (
        "\n\nA room conversation was just transcribed around the user "
        "(speakers S1, S2, …; most recent last). Treat this as live context "
        "for the user's imminent request. You may refer to it, but do not "
        "repeat it back:\n\n" + seed.strip()
    )


def _replay_block(turns: list[dict]) -> str:
    """System-instruction block built from typed replay turns (Phase 5 C+B).

    Ambient entries ("kind"="ambient") render as room-context lines
    ``S1: …``; voice entries render as the classic User/You exchange.
    Empty inputs (or all-empty lines) yield "" so no block is appended.
    """
    if not turns:
        return ""
    lines: list[str] = []
    for t in turns:
        if t.get("kind") == "ambient":
            text = (t.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"S{t.get('speaker', '?')}: {text}")
        else:
            user = (t.get("user_text") or "").strip()
            assistant = (t.get("assistant_text") or "").strip()
            if not (user or assistant):
                continue
            if user and assistant:
                lines.append(f"User said: {user}\nYou replied: {assistant}")
            elif user:
                lines.append(f"User said: {user}")
            else:
                lines.append(f"You replied: {assistant}")
    if not lines:
        return ""
    return (
        "\n\nThe following is context around this conversation (most "
        "recent last): room utterances are tagged S1/S2 (speakers in the "
        "room), 'User' is the person who called you, 'You' is you. Treat "
        "it as things already said; do not repeat or re-introduce "
        "yourself:\n\n"
        + "\n\n".join(lines)
    )


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

    # ── C2 invocation handshake (Phase 5) ──────────────────────────────────
    # Ambient mode ends on-device when "Sirious" is spotted in the room
    # transcript; the phone then opens a voice session with:
    #   seed  = recent diarized room tail ("S1: …/S2: …", capped) → appended
    #           to the system instruction as live room context;
    #   invoke = the trigger utterance ("Sirious, can you answer that?") →
    #           injected to Gemini Live as a text user turn right after
    #           connect, so it answers OUT LOUD without the user repeating
    #           anything to the mic.
    seed = _clean_query_text(websocket.query_params.get("seed"), 4000)
    invoke = _clean_query_text(websocket.query_params.get("invoke"), 500)
    # Phase 6 step 4 (manual client VAD): vad=manual disables the server's
    # automatic activity detection — the CLIENT owns turn boundaries and
    # sends activity_start/activity_end signals relayed below. This removes
    # the server-side ghost class (the model interrupting itself on its own
    # amplified residual). Absent/default → server VAD as before (earphone
    # path unchanged).
    vad_mode = _clean_query_text(websocket.query_params.get("vad"), 20)
    manual_vad = vad_mode == "manual"
    if vad_mode and vad_mode != "manual":
        log_event(session_id, "vad_param_unknown", value=vad_mode)
    ambient_block = _ambient_seed_block(seed)
    if invoke:
        log_event(session_id, "invoke_param", chars=len(invoke))
    if ambient_block:
        log_event(session_id, "ambient_seed_param", chars=len(seed))


    # ── Session resumption ──────────────────────────────────────────────────
    # A client that wants continuity across reconnects sends a stable
    # client_session_id. If we hold a live resumption handle for it, resume
    # the SAME Gemini session; otherwise start fresh.
    resumed = False
    resume_handle = _pop_handle(client_session_id) if client_session_id else None
    if resume_handle is not None:
        resumed = True

    # ── Phase 6 step 5: S2 hardening state ─────────────────────────────────
    # How the CLIENT leg ended (set by the gather below):
    #   "stopped"           — clean client stop/disconnect; Gemini leg is
    #                         torn down eagerly in the finally block.
    #   "recovery_handoff"  — Gemini died while the client was healthy; we
    #                         asked the phone to reconnect (CLOSE_RECOVER)
    #                         and protocol-v2 resumption rebuilds context.
    #   "gemini_closed"     — Gemini died while the client leg was ALSO
    #                         already gone (the common case — nothing to do).
    client_leg_outcome = "stopped"
    # Task handles, pre-initialized so the outer finally is safe even when
    # the Gemini connect itself fails before the tasks are created.
    gemini_task: asyncio.Task | None = None
    client_task: asyncio.Task | None = None
    # Latest resumption handle seen on THIS Gemini leg. On a mid-conversation
    # Gemini death we close the client socket with CLOSE_RECOVER; the phone's
    # blip reconnect then hits _pop_handle above — this cache bridges the
    # race where the newest update has not been stored yet (store happens on
    # every resumable update anyway; the cache is belt-and-braces and feeds
    # the event log's diagnostics).
    last_resumption_handle: str | None = resume_handle
    # Relay guard: collapses duplicate/unbalanced activity signals before
    # they reach Gemini's state machine (the S2 1007 precondition class).
    # Only used when the client owns turn boundaries (vad=manual).
    activity_guard = ActivityWindowGuard() if manual_vad else None

    # Persistence (Phase 2): one Firestore doc per LOGICAL conversation —
    # doc id == client_session_id, so a resuming reconnect EXTENDS the same
    # document rather than creating a new one.
    store = get_store()
    doc_id = client_session_id or session_id

    # Transcript-replay fallback (Phase 2 + Phase 5 C+B): when there is NO
    # live resumption handle (fresh conversation, expired handle, recycled
    # instance, or a voice leg continuing an ambient room doc) but this
    # client_session_id has past turns in Firestore, inject them into the
    # system instruction so the model keeps conversational + room memory.
    # This is awaited BEFORE the Gemini connect, so it never touches the
    # audio path. Typed turns: ambient → "S1: …" room context, voice →
    # User/You exchange.
    replay_block = ""
    if not resumed and client_session_id:
        try:
            replay_entries = await store.replay_turns(doc_id)
        except Exception as e:  # noqa: BLE001 — replay is best-effort
            log_event(session_id, "replay_fetch_error", error=repr(e))
            replay_entries = []
        if replay_entries:
            replay_block = _replay_block(replay_entries)
            log_event(
                session_id,
                "transcript_replay",
                turns_replayed=len(replay_entries),
            )

    # Memory injection (Phase 3): bounded block of durable memories +
    # episodic index into the system instruction. Best-effort — a memory
    # problem must never delay or break the voice path.
    memory = get_memory_store()
    memory_block = ""
    try:
        memory_block = await memory.recall_block()
        if memory_block:
            log_event(session_id, "memory_injected", chars=len(memory_block))
    except Exception as e:  # noqa: BLE001
        log_event(session_id, "memory_recall_error", error=repr(e))

    # (No date/time in the system instruction — a per-session timestamp would
    # churn the prompt prefix on every reconnect and go stale on long/resumed
    # sessions. Temporal grounding lives in tools.py instead: the model passes
    # the user's own words to create_reminder and the server resolves them.)
    # agentic recall AND Phase 4's actions (web_search, add_note) — is
    # declared by app/tools.py per connection and executed through ONE
    # dispatcher in the receive loop below, with an audit record per call.
    # Best-effort: a registry problem must never delay or break the voice path.
    # agentic recall AND Phase 4's actions (web_search, add_note) — is
    # declared by app/tools.py per connection and executed through ONE
    # dispatcher in the receive loop below, with an audit record per call.
    # Best-effort: a registry problem must never delay or break the voice path.
    try:
        registry, audit_log = build_registry(
            session_id=session_id,
            doc_id=doc_id,
            memory=memory,
        )
        tool_declarations = registry.genai_tools()
        tools_hint = ""  # init BEFORE the if — an empty registry must not unbound it
        if tool_declarations:
            # Tool-usage hints: native-audio models trigger declared functions
            # far more reliably when the system instruction tells them WHEN
            # each tool applies (only added for tools actually registered).
            names = set(registry.names())
            hints = []
            if "web_search" in names:
                hints.append(
                    "For current events, news, prices, weather, sports "
                    "scores, or any fact you are unsure about, search the "
                    "web with the web_search tool instead of answering "
                    "from memory."
                )
            if "add_note" in names:
                hints.append(
                    "When the user asks you to note down, save, or capture "
                    "something for later, write it with the add_note tool "
                    "and confirm briefly."
                )
            if "create_reminder" in names:
                hints.append(
                    "For 'remind me to …' requests, always use the two-step "
                    "reminder flow: create_reminder first, read the draft "
                    "(what and when) back to the user, and only call "
                    "confirm_reminder after they clearly say yes."
                )
            tools_hint = (" " + " ".join(hints)) if hints else ""
            log_event(session_id, "tools_registered", tools=registry.names())
    except Exception as e:  # noqa: BLE001
        log_event(session_id, "tools_registry_error", error=repr(e))
        registry, audit_log, tool_declarations = None, None, None
        tools_hint = ""

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

                # Function calling (Phase 3 + Phase 4): all registered tools
                # (search_past_conversations, web_search, add_note …) come
                # from the per-connection registry; None when none registered.
                tools=tool_declarations,

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
                                    "You are Peter, a helpful and concise voice assistant. "
                                    "ALWAYS respond in English, no matter what language the "
                                    "user speaks or is detected as speaking."
                                    + tools_hint
                                    + replay_block
                                    + ambient_block
                                    + memory_block
                                ),

                input_audio_transcription=(
                    types.AudioTranscriptionConfig()
                ),

                output_audio_transcription=(
                    types.AudioTranscriptionConfig()
                ),

                # Phase 6 step 4: manual client VAD (vad=manual handshake).
                # Client owns turn boundaries — activity_start/activity_end
                # arrive as realtime inputs (relayed in client_to_gemini).
                # Docs (live-guide, "Disable automatic VAD", read 30 Aug):
                # client detects speech, sends activityStart/activityEnd;
                # an activityEnd marks the interruption; no audioStreamEnd
                # is used in this mode.
                **(
                    {
                        "realtime_input_config": types.RealtimeInputConfig(
                            automatic_activity_detection=(
                                types.AutomaticActivityDetection(
                                    disabled=True,
                                )
                            ),
                        ),
                    }
                    if manual_vad
                    else {}
                ),
            ),
        ) as session:

            await websocket.send_json({
                "type": "session_started",
                "session_id": session_id,
                "resumed": resumed,
                "vad_mode": "manual" if manual_vad else "server",
            })

            # C2 invocation: the room transcript already contains the user's
            # request ("Sirious, can you answer that?") — inject it as a text
            # user turn so Gemini answers immediately without the user
            # repeating anything into the mic. Best-effort: if injection
            # fails, the phone stays on the normal voice path.
            if invoke:
                try:
                    await session.send_client_content(
                        turns={"role": "user", "parts": [{"text": invoke}]},
                        turn_complete=True,
                    )
                    log_event(session_id, "invoke_injected", chars=len(invoke))
                except Exception as e:  # noqa: BLE001
                    log_event(session_id, "invoke_inject_error", error=repr(e))

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

                            elif manual_vad and control == "activity_start":
                                # Phase 6 step 4: client-detected speech
                                # onset → start of a user activity window.
                                # Step 5: relayed through the window guard —
                                # duplicate starts are suppressed instead of
                                # hitting Gemini's activity state machine
                                # (S2 1007 precondition class).
                                decision = activity_guard.on_start()
                                if decision.forward:
                                    await session.send_realtime_input(
                                        activity_start=types.ActivityStart()
                                    )
                                log_event(
                                    session_id,
                                    "activity_start_relayed"
                                    if decision.forward
                                    else "activity_start_suppressed",
                                    guard=decision.reason,
                                )

                            elif manual_vad and control == "activity_end":
                                decision = activity_guard.on_end()
                                if decision.forward:
                                    await session.send_realtime_input(
                                        activity_end=types.ActivityEnd()
                                    )
                                log_event(
                                    session_id,
                                    "activity_end_relayed"
                                    if decision.forward
                                    else "activity_end_suppressed",
                                    guard=decision.reason,
                                )

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
                nonlocal client_leg_outcome
                nonlocal last_resumption_handle
                try:

                    while True:

                        async for response in session.receive():

                            # -----------------------------
                            # Tool calls (Phase 3 recall + Phase 4 actions)
                            # -----------------------------

                            if response.tool_call:

                                for fc in (
                                    response.tool_call.function_calls
                                    or []
                                ):

                                    args = fc.args if isinstance(
                                        fc.args, dict
                                    ) else {}

                                    log_event(
                                        session_id,
                                        "tool_called",
                                        tool=fc.name,
                                        args={
                                            k: (v if len(str(v)) <= 200 else str(v)[:200] + "…")
                                            for k, v in args.items()
                                        },
                                    )

                                    try:
                                        result_payload = await registry.dispatch(
                                            fc.name,
                                            args,
                                        )
                                        log_event(
                                            session_id,
                                            "tool_result",
                                            tool=fc.name,
                                            outcome=(
                                                "error"
                                                if isinstance(result_payload, dict)
                                                and result_payload.get("error")
                                                else "ok"
                                            ),
                                            error=(
                                                result_payload.get("error")
                                                if isinstance(result_payload, dict) and result_payload.get("error")
                                                else None
                                            ),
                                        )
                                    except Exception as e:  # noqa: BLE001 — belt & braces; dispatch already degrades
                                        log_event(
                                            session_id,
                                            "tool_error",
                                            tool=fc.name,
                                            error=repr(e),
                                        )
                                        result_payload = {
                                            "error": "tool execution failed"
                                        }

                                    await session.send_tool_response(
                                        function_responses=[
                                            types.FunctionResponse(
                                                name=fc.name,
                                                # REQUIRED by Google AI: echo the
                                                # call id back, else the request
                                                # 400s inside the Live session.
                                                id=fc.id,
                                                response=result_payload,
                                            )
                                        ]
                                    )
                                continue

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

                                    # Step 5: remember it for the recovery
                                    # path (see client_leg_outcome docs).
                                    last_resumption_handle = (
                                        update.new_handle
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

                    # ── Phase 6 step 5: transparent Gemini-leg recovery ──
                    # Gemini sometimes kills the session mid-conversation
                    # while the CLIENT leg is perfectly healthy (S2, 31 Aug:
                    # rapid barge-ins → close 1007 "Precondition check
                    # failed"). The phone cannot see this leg, so it never
                    # reconnects on its own — the conversation just dies.
                    # Detect "client still attached" by trying to send the
                    # error frame: a live socket accepts it, a dead one
                    # raises. On a live client, hand the phone to its own
                    # battle-tested blip-reconnect (Phase 1) instead of
                    # letting the conversation die silently: we signal
                    # CLOSE_RECOVER and exit — the phone re-dials in ~1-2 s
                    # with the SAME client_session_id, and protocol-v2
                    # resumption (resume handle stored on every update)
                    # rebuilds the SAME Gemini conversation. To the user
                    # this shows as a brief "reconnecting…" flash, not a
                    # dead session.
                    client_still_attached = False
                    with contextlib.suppress(Exception):
                        await websocket.send_json({
                            "type": "error",
                            "code": "GEMINI_ERROR",
                            "message": str(e),
                        })
                        client_still_attached = True

                    if client_still_attached:
                        client_leg_outcome = "recovery_handoff"
                        log_event(
                            session_id,
                            "gemini_leg_recovery_handoff",
                            handle_available=last_resumption_handle
                            is not None,
                        )
                        with contextlib.suppress(Exception):
                            await websocket.send_json({
                                "type": "session_recovering",
                            })
                            await websocket.close(
                                code=CLOSE_RECOVER,
                                reason="gemini leg lost — reconnect to resume",
                            )
                    # Client gone too: the common case, nothing to recover.

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
            client_task.set_name("sirious:client_to_gemini")
            gemini_task.set_name("sirious:gemini_to_client")

            try:

                await asyncio.gather(
                    client_task,
                    gemini_task,
                )

            finally:

                # Record HOW the client leg ended (drives the finally-block
                # teardown policy below). Cancellation or an exception on
                # the client task means the client leg is already gone —
                # there is no "stop" to race a Gemini teardown against.
                if client_task.cancelled() or (
                    client_task.done()
                    and client_task.exception() is not None
                ):
                    client_leg_outcome = "gemini_closed"

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
            traceback=_tb.format_exc()[-1800:],
        )
    finally:

        # If the client/session disappears before Gemini
        # sends turn_complete, don't lose the accumulated turn.
        if turn_id is not None:

            finish_turn(
                "session_ended"
            )

        # Phase 6 step 5: when the CLIENT leg ended by deliberate stop or
        # clean disconnect, tear the Gemini leg down immediately instead of
        # leaving it draining (~2.5 min until Gemini's own "1008 aborted"
        # cleanup — measured on prod logs 31 Aug). The receive loop's
        # CancelledError path exits the connect context manager, which
        # closes the Gemini session cleanly. The stored resumption handle
        # is untouched (it was persisted on every update), so a phone that
        # disconnected for a blip still resumes on reconnect. A recovery
        # handoff ("recovery_handoff") needs no teardown — the Gemini leg
        # is already gone, and the phone is on its way back.
        if (
            client_leg_outcome == "stopped"
            and gemini_task is not None
            and not gemini_task.done()
        ):
            gemini_task.cancel()
            await asyncio.gather(gemini_task, return_exceptions=True)

        # Finalize the Firestore doc (Phase 2). The queued session_end op
        # carries the full buffered turns array, so even if the instance is
        # killed right now every completed turn was already written
        # turn-by-turn above.
        store.end_session(
            doc_id,
            now_iso=now(),
            reason=(
                "recovered_via_reconnect"
                if client_leg_outcome == "recovery_handoff"
                else "session_ended"
            ),
            audio_in_bytes=audio_in_bytes,
            audio_out_bytes=audio_out_bytes,
        )

        # Memory extraction (Phase 3): fire-and-forget request. The handler
        # snapshots its buffered turns synchronously (all segments of a
        # resumed conversation included) and hands them to the memory writer,
        # so extraction never races the Phase 2 Firestore writer.
        try:
            memory.request_extraction(doc_id, store.snapshot_turns(doc_id))
        except Exception as e:  # noqa: BLE001 — never break teardown
            log_event(session_id, "memory_extract_request_error", error=repr(e))

        log_event(
            session_id,
            "session_ended",
            audio_in_bytes=audio_in_bytes,
            audio_out_bytes=audio_out_bytes,
        )