"""Contextual memory for Sirious (Phase 3).

Pipeline
--------
    stored turns (Phase 2) ── session end ──► extraction job (one flash-model
    call per session) ──► structured memory docs (+ embeddings) ──► retrieval
    at next session start (injected into system_instruction)

Design rules (mirroring store.py)
---------------------------------
- All WebSocket-hot-path methods are SYNCHRONOUS and non-blocking: extraction
  is requested by enqueueing; ONE background writer task owns every model call
  and Firestore write. A memory problem must NEVER take down the voice path —
  every method swallows and logs its own exceptions.
- ``SIRIOUS_MEMORY != "1"`` selects NullMemoryStore so local dev runs the
  exact same hot path with zero GCP/Gemini dependencies.
- Extraction is WATERMARKED per session doc (``memory_meta/{doc_id}`` →
  ``extracted_turn_count``), so re-extraction after a resume/crash is cheap
  and dedup keeps memories clean.
- DEDUP: cosine similarity ≥ DEDUP_COSINE_MIN against any active memory →
  append provenance to the existing doc instead of inserting a duplicate.
- Retrieval is exact in-process cosine ranking over active memories. At
  personal scale (hundreds–low thousands) this beats standing up Firestore
  vector indexes: no infra, exact scores.
"""

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("sirious.memory")

MEMORIES_COLLECTION = "memories"
META_COLLECTION = "memory_meta"
SESSIONS_COLLECTION = "sessions"

EXTRACT_MODEL = os.environ.get("SIRIOUS_EXTRACT_MODEL", "gemini-2.5-flash")
EMBED_MODEL = os.environ.get("SIRIOUS_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.environ.get("SIRIOUS_EMBED_DIM", "768"))

DEDUP_COSINE_MIN = 0.90          # ≥ this → same memory, extend provenance
MAX_ACTIVE_MEMORIES = 2000       # scan cap for dedup/retrieval
MAX_MEMORY_TEXT_CHARS = 400      # per-memory text cap
MAX_TOPICS = 12                  # per memory
MAX_ENTITIES = 12                # per memory
MAX_PROVENANCE_ENTRIES = 20      # per memory
FACTS_IN_INJECT = 8              # semantic+task facts injected at session start
EPISODES_IN_INJECT = 12          # episodic index lines injected
INJECT_MAX_CHARS = 2400          # hard bound on the injected memory block


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ms() -> int:
    return time.time_ns() // 1_000_000


def _cosine(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine — exact, no numpy dep. Personal scale is fine."""
    if not a or not b:
        return 0.0
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def _clip(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


# ── Extraction schema ────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """\
You extract durable memories from a transcript of a voice-assistant \
conversation. The transcript lines are numbered; refer to turns by number.

Return memories of these types:
- "episodic": what the conversation was ABOUT, one per distinct subject, \
phrased as "User and assistant discussed …". Include these even for casual \
questions and trivia — the product must be able to answer "did we ever talk \
about X?" for ANYTHING discussed, however casual. Put broad subject \
categories in "topics" (e.g. a discussion of peacock colors → topics: \
["birds", "peacocks", "animals", "colors"]) so a question about the broader \
topic ("birds") retrieves it.
- "semantic": durable facts, preferences, dates, plans, project details \
about the user worth remembering weeks later.
- "entity": notable people, companies, projects, places mentioned (names \
plus one-line context).
- "task": action items, commitments, reminders ("follow up with X on Y").

Rules:
- Be selective for semantic/entity/task (skip chit-chat, greetings, \
meta-commentary about the assistant itself) — but NEVER skip episodic.
- "text" is one self-contained sentence understandable months later.
- "turn_refs": numbers of the transcript turns the memory came from.
"""


def _extraction_schema() -> Any:
    from google.genai import types

    str_array = types.Schema(
        type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING)
    )
    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "memories": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "type": types.Schema(
                            type=types.Type.STRING,
                            enum=["episodic", "semantic", "entity", "task"],
                        ),
                        "text": types.Schema(type=types.Type.STRING),
                        "topics": str_array,
                        "entities": str_array,
                        "turn_refs": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.INTEGER),
                        ),
                    },
                    required=["type", "text"],
                ),
            )
        },
        required=["memories"],
    )


class MemoryStore:
    """Firestore + Gemini backed memory store (SIRIOUS_MEMORY=1)."""

    def __init__(self) -> None:
        self._db: Any = None            # google.cloud.firestore.AsyncClient
        self._genai: Any = None         # google.genai.Client
        self._queue: asyncio.Queue | None = None
        self._writer_task: asyncio.Task | None = None

    def enabled(self) -> bool:
        return True

    # ── lazy clients ────────────────────────────────────────────────────────

    def _ensure_db(self) -> Any:
        if self._db is None:
            from google.cloud.firestore import AsyncClient

            project = os.environ.get("GCP_PROJECT") or os.environ.get(
                "GOOGLE_CLOUD_PROJECT"
            )
            kwargs: dict[str, Any] = {}
            if project:
                kwargs["project"] = project
            self._db = AsyncClient(**kwargs)
        return self._db

    def _ensure_genai(self) -> Any:
        if self._genai is None:
            from google import genai

            self._genai = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        return self._genai

    # ── queue + single writer (never blocks the voice path) ────────────────

    def _enqueue(self, op: tuple[str, str, Any]) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue()
            self._writer_task = asyncio.create_task(
                self._writer(), name="sirious-memory-writer"
            )
        self._queue.put_nowait(op)

    async def _writer(self) -> None:
        while True:
            kind, doc_id, payload = await self._queue.get()
            try:
                if kind == "extract":
                    await self._apply_extract(doc_id, payload)
                else:
                    log.warning("unknown memory op %r", kind)
            except Exception:  # noqa: BLE001 — never propagate anywhere
                log.exception("memory op failed kind=%s doc=%s", kind, doc_id)
            finally:
                self._queue.task_done()

    # ── hot-path API (synchronous, non-blocking) ────────────────────────────

    def request_extraction(self, doc_id: str, turns: list[dict[str, Any]]) -> None:
        """Called from the WS finally-block right after end_session.

        ``turns`` is the caller's in-memory snapshot of the conversation
        ([{id, user_text, assistant_text}, …]). Snapshotting HERE — before
        the Phase 2 writer applies its queue — avoids any read-after-write
        race with Firestore: the extractor never re-reads the session doc.
        """
        self._enqueue(("extract", doc_id, turns))

    # ── extraction ──────────────────────────────────────────────────────────

    async def _extract_with_model(self, numbered: str) -> list[dict[str, Any]]:
        client = self._ensure_genai()
        resp = await client.aio.models.generate_content(
            model=EXTRACT_MODEL,
            contents=numbered,
            config={
                "system_instruction": EXTRACTION_PROMPT,
                "response_mime_type": "application/json",
                "response_schema": _extraction_schema(),
                "temperature": 0.1,
            },
        )
        raw = getattr(resp, "parsed", None)
        if raw is None:
            raw = json.loads(resp.text or "{}")
        memories = (raw or {}).get("memories") or []
        return [m for m in memories if isinstance(m, dict) and (m.get("text") or "").strip()]

    async def _apply_extract(
        self, doc_id: str, turn_snapshot: list[dict[str, Any]]
    ) -> None:
        db = self._ensure_db()

        turns = [
            t for t in (turn_snapshot or [])
            if (t.get("user_text") or t.get("assistant_text"))
        ]
        if not turns:
            return

        meta_ref = db.collection(META_COLLECTION).document(doc_id)
        meta_snap = await meta_ref.get()
        already_done: set[str] = set()
        if meta_snap.exists:
            already_done = set(
                (meta_snap.to_dict() or {}).get("extracted_turn_ids") or []
            )
        # Turn-ID based watermark (not a count): a resumed session's handler
        # snapshot contains ALL turns of the conversation, while a count
        # watermark from an earlier segment would wrongly skip the head.
        new_turns = [t for t in turns if t.get("id") not in already_done]
        if not new_turns:
            return

        numbered = "\n".join(
            f"{i}. User: {t.get('user_text') or ''}\n"
            f"   Assistant: {t.get('assistant_text') or ''}"
            for i, t in enumerate(new_turns, start=1)
        )

        memories = await self._extract_with_model(numbered)

        started_at = None
        try:
            sess_snap = await (
                db.collection(SESSIONS_COLLECTION)
                .document(doc_id)
                .get()
            )
            if sess_snap.exists:
                # Best-effort metadata; extraction works without it.
                started_at = (sess_snap.to_dict() or {}).get("started_at")
        except Exception:  # noqa: BLE001 — provenance date is optional
            pass

        session_info = {
            "session_ref": doc_id,
            "started_at": started_at,
            "title": _clip(
                (turns[0].get("user_text") or "").strip(), MAX_TITLE_CHARS_MEM
            ),
        }
        for m in memories:
            turn_ids = []
            for ref in (m.get("turn_refs") or []):
                if isinstance(ref, int) and 1 <= ref <= len(new_turns):
                    turn_ids.append(new_turns[ref - 1].get("id"))
            await self._upsert_memory(m, session_info, turn_ids)

        # Advance the watermark ONLY on success — a failed extraction retries
        # naturally at the next session end on this doc.
        await meta_ref.set(
            {
                "extracted_turn_ids": sorted(
                    {*(already_done), *(t.get("id") for t in new_turns)}
                ),
                "updated_at": _now_iso(),
            },
            merge=True,
        )
        log.info(
            "extraction applied doc=%s new_turns=%d memories=%d",
            doc_id, len(new_turns), len(memories),
        )

    # ── storage helpers ─────────────────────────────────────────────────────

    async def _embed(self, texts: list[str], *, task_type: str) -> list[list[float]]:
        client = self._ensure_genai()
        # NOTE: embed_content is SYNCHRONOUS in google-genai (only
        # client.aio.models.* are coroutines). It does blocking HTTP under
        # the hood, but this runs only inside our background writer task,
        # never on the voice hot path.
        resp = client.models.embed_content(
            model=EMBED_MODEL,
            contents=texts,
            config={
                "task_type": task_type,
                "output_dimensionality": EMBED_DIM,
            },
        )
        return [list(e.values or []) for e in (resp.embeddings or [])]

    async def _load_active(self) -> list[dict[str, Any]]:
        """Active (non-deleted) memories, newest first. Client-side delete
        filter on purpose: dodges FieldFilter version differences and keeps
        the test fakes trivial."""
        snaps = (
            self._ensure_db()
            .collection(MEMORIES_COLLECTION)
            .order_by("created_ms", direction="DESCENDING")
            .limit(MAX_ACTIVE_MEMORIES)
            .stream()
        )
        out = []
        async for s in snaps:
            d = s.to_dict() or {}
            if d.get("deleted"):
                continue
            d["id"] = s.id
            out.append(d)
        return out

    @staticmethod
    def _norm_text(text: str) -> str:
        return " ".join((text or "").split())[:MAX_MEMORY_TEXT_CHARS]

    async def _upsert_memory(
        self,
        m: dict[str, Any],
        session_info: dict[str, Any],
        turn_ids: list[str],
    ) -> None:
        db = self._ensure_db()
        text = self._norm_text(m.get("text"))
        if not text:
            return
        mtype = m.get("type") if m.get("type") in (
            "episodic", "semantic", "entity", "task",
        ) else "semantic"
        topics = [_clip(t, 60) for t in (m.get("topics") or [])][:MAX_TOPICS]
        entities = [_clip(e, 80) for e in (m.get("entities") or [])][:MAX_ENTITIES]

        (embedding,) = await self._embed([text], task_type="RETRIEVAL_DOCUMENT")

        provenance_entry = {
            "session_ref": session_info["session_ref"],
            "started_at": session_info.get("started_at"),
            "title": _clip(session_info.get("title"), MAX_TITLE_CHARS_MEM),
            "turn_ids": turn_ids[:10],
        }

        # Dedup: nearest active memory by cosine on text embedding.
        best = None
        best_sim = 0.0
        for cand in await self._load_active():
            sim = _cosine(embedding, cand.get("embedding") or [])
            if sim > best_sim:
                best, best_sim = cand, sim

        now_iso = _now_iso()
        if best is not None and best_sim >= DEDUP_COSINE_MIN:
            prov = list(best.get("provenance") or [])
            if provenance_entry not in prov:
                prov.append(provenance_entry)
            prov = prov[-MAX_PROVENANCE_ENTRIES:]
            merged_topics = list(
                dict.fromkeys((best.get("topics") or []) + topics)
            )[:MAX_TOPICS]
            merged_entities = list(
                dict.fromkeys((best.get("entities") or []) + entities)
            )[:MAX_ENTITIES]
            await (
                db.collection(MEMORIES_COLLECTION)
                .document(best["id"])
                .set(
                    {
                        "provenance": prov,
                        "times_seen": int(best.get("times_seen", 1)) + 1,
                        "last_seen_at": now_iso,
                        "topics": merged_topics,
                        "entities": merged_entities,
                    },
                    merge=True,
                )
            )
            log.info(
                "memory deduped into %s (sim=%.3f) text=%r",
                best["id"], best_sim, text[:60],
            )
            return

        doc = {
            "type": mtype,
            "text": text,
            "topics": topics,
            "entities": entities,
            "speaker": None,          # Phase 5+: who said it (multi-user)
            "embedding": embedding,
            "provenance": [provenance_entry],
            "times_seen": 1,
            "created_at": now_iso,
            "created_ms": _ms(),
            "last_seen_at": now_iso,
            "deleted": False,
            "deleted_at": None,
            "schema_version": 1,
        }
        await db.collection(MEMORIES_COLLECTION).add(doc)

    # ── retrieval ───────────────────────────────────────────────────────────

    async def recall(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        """Top-K active memories ranked by cosine vs the query embedding."""
        if not query.strip():
            return []
        (qv,) = await self._embed([query], task_type="RETRIEVAL_QUERY")
        scored = []
        for cand in await self._load_active():
            scored.append((_cosine(qv, cand.get("embedding") or []), cand))
        scored.sort(key=lambda p: p[0], reverse=True)
        return [
            {
                "id": c["id"],
                "type": c.get("type"),
                "text": c.get("text"),
                "topics": c.get("topics") or [],
                "entities": c.get("entities") or [],
                "score": round(s, 4),
                "provenance": (c.get("provenance") or [])[-3:],
            }
            for s, c in scored[:top_k]
        ]

    async def recall_block(self) -> str:
        """Bounded memory block for injection into system_instruction at
        session start (query-less: newest facts + episodic index)."""
        active = await self._load_active()

        facts = [
            m for m in active
            if m.get("type") in ("semantic", "task")
        ][:FACTS_IN_INJECT]
        episodes = [
            m for m in active if m.get("type") == "episodic"
        ][:EPISODES_IN_INJECT]

        lines: list[str] = []
        if facts:
            lines.append("Things you remember about the user:")
            lines += [f"- {m['text']}" for m in facts]
        if episodes:
            lines.append("Recent conversations you had with them:")
            lines += [f"- {_episode_line(m)}" for m in episodes]
        if not lines:
            return ""

        block = (
            "\n\nLong-term memory (background context about this user and "
            "your past conversations; use it naturally, don't recite it):\n"
            + "\n".join(lines)
        )
        return block[:INJECT_MAX_CHARS]

    # ── user controls (REST) ────────────────────────────────────────────────

    async def list_memories(self, limit: int = 100) -> list[dict[str, Any]]:
        out = []
        for m in (await self._load_active())[:limit]:
            out.append(
                {
                    "id": m["id"],
                    "type": m.get("type"),
                    "text": m.get("text"),
                    "topics": m.get("topics") or [],
                    "entities": m.get("entities") or [],
                    "speaker": m.get("speaker"),
                    "created_at": m.get("created_at"),
                    "last_seen_at": m.get("last_seen_at"),
                    "times_seen": m.get("times_seen", 1),
                    "provenance": (m.get("provenance") or [])[-5:],
                }
            )
        return out

    async def soft_delete(self, memory_id: str) -> bool:
        db = self._ensure_db()
        ref = db.collection(MEMORIES_COLLECTION).document(memory_id)
        snap = await ref.get()
        if not snap.exists:
            return False
        await ref.set(
            {"deleted": True, "deleted_at": _now_iso()},
            merge=True,
        )
        return True


MAX_TITLE_CHARS_MEM = 80


def _episode_line(m: dict[str, Any]) -> str:
    """Compact index line: '22 Aug 2026 — discussed peacock colors'."""
    stamp = ""
    started = (m.get("provenance") or [{}])[-1].get("started_at")
    if started:
        try:
            dt = datetime.fromisoformat(str(started))
            stamp = dt.strftime("%d %b %Y").lstrip("0") + " — "
        except ValueError:
            pass
    return f"{stamp}{m.get('text', '')}"


class NullMemoryStore:
    """No-op stand-in used when SIRIOUS_MEMORY != "1"."""

    def enabled(self) -> bool:
        return False

    def request_extraction(self, doc_id: str) -> None: ...

    async def recall(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        return []

    async def recall_block(self) -> str:
        return ""

    async def list_memories(self, limit: int = 100) -> list[dict[str, Any]]:
        return []

    async def soft_delete(self, memory_id: str) -> bool:
        return False


_store: MemoryStore | NullMemoryStore | None = None


def get_memory_store() -> MemoryStore | NullMemoryStore:
    global _store
    if _store is None:
        _store = (
            MemoryStore()
            if os.environ.get("SIRIOUS_MEMORY") == "1"
            else NullMemoryStore()
        )
    return _store


def reset_memory_store_for_tests() -> None:
    global _store
    _store = None
