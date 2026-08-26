"""Firestore persistence for Sirious sessions (Phase 2).

Design notes
------------
- One Firestore document per LOGICAL conversation. Doc id ==
  ``client_session_id`` (protocol v2) when the client supplies one, else
  the server-side session id. A reconnect that resumes the same Gemini
  session therefore extends the SAME document.
- All writes go through an in-process asyncio queue consumed by ONE
  background writer task. Nothing in the WebSocket hot path ever awaits
  Firestore — enqueue is synchronous and non-blocking; worst case the
  queue grows while Firestore is slow.
- Turns are buffered in memory per session and the whole ``turns`` array
  is rewritten on each recorded turn (a few KB at personal scale).
  Simpler and race-free versus arrayUnion upserts, and bounds loss on an
  instance kill to the in-flight turn only (turn-level writes, not a
  session-end bulk dump).
- Every store method swallows its own exceptions and logs them: a
  persistence problem must NEVER take down the live voice path.
- ``SIRIOUS_PERSIST != "1"`` selects NullSessionStore (no-op) so local
  dev runs exactly the hot-path code with zero GCP dependencies.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any

log = logging.getLogger("sirious.store")

SESSIONS_COLLECTION = "sessions"
MAX_TITLE_CHARS = 80


def _ms() -> int:
    """Epoch milliseconds — used for range scans / ordering."""
    return time.time_ns() // 1_000_000


def make_title(first_user_text: str | None) -> str | None:
    """History-list title: first user utterance, truncated."""
    if not first_user_text:
        return None
    text = " ".join(first_user_text.split())
    if not text:
        return None
    if len(text) > MAX_TITLE_CHARS:
        text = text[: MAX_TITLE_CHARS - 1] + "…"
    return text


class SessionStore:
    """Firestore-backed store. Lazily connects; safe if never awaited."""

    def __init__(self) -> None:
        self._db: Any = None            # google.cloud.firestore.AsyncFirestoreClient
        self._buffers: dict[str, dict[str, Any]] = {}
        self._queue: asyncio.Queue | None = None
        self._writer_task: asyncio.Task | None = None

    # ── internals ────────────────────────────────────────────────────────

    def _ensure_db(self) -> Any:
        if self._db is None:
            # Imported lazily so environments without the package (or
            # without ADC) can still import this module for Null-mode use.
            from google.cloud.firestore import AsyncClient

            project = os.environ.get("GCP_PROJECT") or os.environ.get(
                "GOOGLE_CLOUD_PROJECT"
            )
            kwargs: dict[str, Any] = {}
            if project:
                kwargs["project"] = project
            self._db = AsyncClient(**kwargs)
        return self._db

    def _enqueue(self, kind: str, doc_id: str, payload: dict[str, Any]) -> None:
        """Queue a write. Starts the writer task on first use."""
        if self._queue is None:
            self._queue = asyncio.Queue()
            self._writer_task = asyncio.create_task(
                self._writer(), name="sirious-store-writer"
            )
        self._queue.put_nowait((kind, doc_id, payload))

    async def _writer(self) -> None:
        """Single consumer: applies queued ops sequentially, forever."""
        while True:
            kind, doc_id, payload = await self._queue.get()
            try:
                if kind == "session_start":
                    await self._apply_start(doc_id, payload)
                elif kind == "turn":
                    await self._apply_turn(doc_id, payload)
                elif kind == "session_end":
                    await self._apply_end(doc_id, payload)
                else:
                    log.warning("unknown store op %r", kind)
            except Exception:  # noqa: BLE001 — never propagate into the voice path
                log.exception(
                    "store write failed kind=%s doc=%s", kind, doc_id
                )
            finally:
                self._queue.task_done()

    def _buffer(self, doc_id: str) -> dict[str, Any]:
        buf = self._buffers.setdefault(doc_id, {"turns": []})
        buf.setdefault("turns", [])
        return buf

    def _seed_start_ms(self, doc_id: str, ms: int) -> None:
        self._buffer(doc_id)["started_ms"] = ms

    async def _apply_start(self, doc_id: str, p: dict[str, Any]) -> None:
        db = self._ensure_db()
        ref = db.collection(SESSIONS_COLLECTION).document(doc_id)
        snap = await ref.get()
        if snap.exists:
            # Resumed reconnect: extend the existing conversation document.
            await ref.set(
                {
                    "updated_ms": p["updated_ms"],
                    "last_updated_at": p["now_iso"],
                    "resume_count": snap.to_dict().get("resume_count", 0) + 1,
                },
                merge=True,
            )
        else:
            await ref.set(p["doc"])
        log.info("session_start applied doc=%s", doc_id)

    async def _apply_turn(self, doc_id: str, p: dict[str, Any]) -> None:
        db = self._ensure_db()
        buf = self._buffer(doc_id)

        # Replace-or-append by turn id (finish_turn is the only producer,
        # ids are unique, but a retried op must not duplicate a turn).
        buf["turns"] = [
            t for t in buf.get("turns", []) if t.get("id") != p["turn"]["id"]
        ]
        buf["turns"].append(p["turn"])

        merge = {
            "turns": buf["turns"],
            "turn_count": len(buf["turns"]),
            "updated_ms": p["updated_ms"],
            "last_updated_at": p["now_iso"],
        }
        # Optional extras (e.g. first-turn title) ride along here.
        for k in ("title",):
            if p.get(k) is not None:
                merge[k] = p[k]

        await db.collection(SESSIONS_COLLECTION).document(doc_id).set(
            merge, merge=True
        )

    async def _apply_end(self, doc_id: str, p: dict[str, Any]) -> None:
        db = self._ensure_db()
        # Read the buffer at APPLY time, not enqueue time: every queued
        # turn op has been applied by this sequential writer already, so
        # the buffer holds exactly the turns Firestore should end with.
        # (Snapshotting at enqueue time raced ahead of pending turn ops.)
        turns = self._buffers.get(doc_id, {}).get("turns", [])
        await db.collection(SESSIONS_COLLECTION).document(doc_id).set(
            {**p["fields"], "turns": turns},
            merge=True,
        )
        self._buffers.pop(doc_id, None)
        log.info("session_end applied doc=%s turns=%d", doc_id, len(turns))

    # ── hot-path API (all synchronous, non-blocking) ─────────────────────

    def start_session(
        self,
        doc_id: str,
        *,
        client_session_id: str | None,
        model: str,
        resumed: bool,
        device: str | None,
        now_iso: str,
        mode: str = "voice",
    ) -> None:
        ms = _ms()
        self._seed_start_ms(doc_id, ms)
        self._enqueue(
            "session_start",
            doc_id,
            {
                "updated_ms": ms,
                "now_iso": now_iso,
                "doc": {
                    "client_session_id": client_session_id,
                    "model": model,
                    "device": device,
                    "mode": mode,
                    "started_at": now_iso,
                    "started_ms": _ms(),
                    "updated_ms": _ms(),
                    "last_updated_at": now_iso,
                    "ended_at": None,
                    "end_reason": None,
                    "duration_s": None,
                    "resume_count": 1 if resumed else 0,
                    "turn_count": 0,
                    "turns": [],
                    "title": None,
                    "schema_version": 1,
                },
            },
        )

    def record_ambient_turn(
        self,
        doc_id: str,
        *,
        speaker_tag: int,
        text: str,
        start_s: float,
        end_s: float,
        now_iso: str,
    ) -> None:
        """Phase 5 ambient mode: one diarized utterance = one turn."""
        turn = {
            "id": f"amb-{_ms()}-{speaker_tag}",
            "kind": "ambient",
            "speaker": int(speaker_tag),
            "text": text,
            "start_s": round(float(start_s), 2),
            "end_s": round(float(end_s), 2),
            "ended_at": now_iso,
        }
        buf = self._buffer(doc_id)
        if not buf.get("title") and text.strip():
            buf["title"] = make_title(text)
            buf["title_pending"] = True
        extra: dict[str, Any] = {}
        if buf.pop("title_pending", False) and buf.get("title"):
            extra["title"] = buf["title"]
        self._enqueue(
            "turn",
            doc_id,
            {"turn": turn, "updated_ms": _ms(), "now_iso": now_iso, **extra},
        )

    def record_turn(self, doc_id: str, *, summary: dict[str, Any], now_iso: str) -> None:
        """summary = the turn_summary log record minus envelope keys."""
        turn = {
            "id": summary["turn_id"],
            "started_at": summary.get("started_at"),
            "ended_at": summary.get("ended_at"),
            "user_text": summary.get("user_text") or "",
            "assistant_text": summary.get("assistant_text") or "",
            "interrupted": bool(summary.get("interrupted")),
            "reason": summary.get("reason"),
            "audio_in_bytes": summary.get("audio_in_bytes"),
            "audio_out_bytes": summary.get("audio_out_bytes"),
        }
        buf = self._buffer(doc_id)
        if not buf.get("title"):
            buf["title"] = make_title(turn["user_text"])
            buf["title_pending"] = True

        extra: dict[str, Any] = {}
        if buf.pop("title_pending", False) and buf.get("title"):
            extra["title"] = buf["title"]

        self._enqueue(
            "turn",
            doc_id,
            {"turn": turn, "updated_ms": _ms(), "now_iso": now_iso, **extra},
        )

    def end_session(
        self,
        doc_id: str,
        *,
        now_iso: str,
        reason: str,
        audio_in_bytes: int,
        audio_out_bytes: int,
    ) -> None:
        started_ms = self._buffers.get(doc_id, {}).get("started_ms")
        duration = (
            round((time.time_ns() // 1_000_000 - started_ms) / 1000, 1)
            if started_ms
            else None
        )
        # NOTE: turns are NOT snapshotted here — _apply_end reads the buffer
        # at apply time so the sequential writer never clobbers pending turns.
        self._enqueue(
            "session_end",
            doc_id,
            {
                "fields": {
                    "ended_at": now_iso,
                    "end_reason": reason,
                    "duration_s": duration,
                    "total_audio_in_bytes": audio_in_bytes,
                    "total_audio_out_bytes": audio_out_bytes,
                    "updated_ms": _ms(),
                    "last_updated_at": now_iso,
                },
            },
        )

    def snapshot_turns(self, doc_id: str) -> list[dict[str, Any]]:
        """In-memory snapshot of every turn buffered for this doc (all
        segments of a resumed conversation included). Read synchronously by
        the WS handler at teardown — before queued writes are applied — and
        handed to the memory extractor so IT never races the Phase 2 writer.
        """
        turns = self._buffer(doc_id).get("turns", [])
        return [
            {"id": t.get("id"),
             "user_text": t.get("user_text") or "",
             "assistant_text": t.get("assistant_text") or ""}
            for t in turns
        ]

    async def delete_session(self, doc_id: str) -> bool:
        """Hard-delete a conversation document. Returns False if absent."""
        db = self._ensure_db()
        ref = db.collection(SESSIONS_COLLECTION).document(doc_id)
        snap = await ref.get()
        if not snap.exists:
            return False
        await ref.delete()
        self._buffers.pop(doc_id, None)
        return True

    # ── read API (REST endpoints; these DO await) ────────────────────────

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        db = self._ensure_db()
        cols = db.collection(SESSIONS_COLLECTION)
        snaps = (
            cols.order_by("updated_ms", direction="DESCENDING")
            .limit(limit)
            .stream()
        )
        out = []
        async for s in snaps:
            d = s.to_dict() or {}
            turns = d.get("turns") or []
            out.append(
                {
                    "id": s.id,
                    "title": d.get("title"),
                    "preview": (turns[-1]["assistant_text"][:140]
                                if turns and turns[-1].get("assistant_text") else None),
                    "started_at": d.get("started_at"),
                    "ended_at": d.get("ended_at"),
                    "duration_s": d.get("duration_s"),
                    "turn_count": d.get("turn_count", len(turns)),
                    "model": d.get("model"),
                    "updated_ms": d.get("updated_ms"),
                }
            )
        return out

    async def get_session(self, doc_id: str) -> dict[str, Any] | None:
        db = self._ensure_db()
        snap = await db.collection(SESSIONS_COLLECTION).document(doc_id).get()
        if not snap.exists:
            return None
        return snap.to_dict()

    async def replay_turns(
        self, doc_id: str, limit: int = 12
    ) -> list[dict[str, str]]:
        """Most recent turns (oldest→newest) for resume-context replay."""
        doc = await self.get_session(doc_id)
        if not doc:
            return []
        turns = doc.get("turns") or []
        return [
            {
                "user_text": t.get("user_text") or "",
                "assistant_text": t.get("assistant_text") or "",
            }
            for t in turns[-limit:]
            if (t.get("user_text") or t.get("assistant_text"))
        ]


class NullSessionStore:
    """No-op stand-in used when SIRIOUS_PERSIST != "1"."""

    def start_session(self, *a: Any, **k: Any) -> None: ...
    def record_turn(self, *a: Any, **k: Any) -> None: ...
    def record_ambient_turn(self, *a: Any, **k: Any) -> None: ...
    def end_session(self, *a: Any, **k: Any) -> None: ...
    def snapshot_turns(self, doc_id: str) -> list[dict[str, Any]]:
        return []

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return []

    async def get_session(self, doc_id: str) -> None:
        return None

    async def replay_turns(
        self, doc_id: str, limit: int = 12
    ) -> list[dict[str, str]]:
        return []

    async def delete_session(self, doc_id: str) -> bool:
        return False


_store: SessionStore | None = None


def get_store() -> SessionStore | NullSessionStore:
    """Process-wide store, selected once by env var."""
    global _store
    if _store is None:
        _store = SessionStore() if os.environ.get("SIRIOUS_PERSIST") == "1" else NullSessionStore()
    return _store


def reset_store_for_tests() -> None:
    global _store
    _store = None
