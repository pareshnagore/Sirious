"""Server-side tool registry for Sirious (Phase 4 — tools & actions).

Design notes
------------
- Gemini Live stays the voice front door: the model emits function calls,
  THIS module owns the registry, execution, and audit trail. Nothing here
  ever touches the audio path directly — handlers return plain dicts that
  ``main.py`` sends back as ``FunctionResponse`` payloads.
- One registry instance is built per WebSocket connection (handlers close
  over that conversation's ids/stores). Declarations are static per build;
  which tools exist depends on environment configuration, mirroring the
  Phase 3 pattern (SIRIOUS_MEMORY gates memory tools):
      SIRIOUS_TOOLS=1            → master gate for Phase 4 tools
      TAVILY_API_KEY set         → web_search registers
      SIRIOUS_PERSIST=1          → add_note registers (needs Firestore)
  search_past_conversations keeps its Phase 3 gate (memory enabled).
- Audit: EVERY invocation (ok, error, unknown tool, unconfigured) lands in
  the ``tool_audit`` Firestore collection as its own doc, written
  fire-and-forget so audit failures can never break the voice path.
  Local/null mode audits into an in-memory ring buffer instead.
- Confirmation scaffold: specs may declare ``requires_confirmation=True``.
  No shipped tool sets it yet (both Phase 4 v1 tools are read/append-only);
  the first destructive tool (create_reminder etc.) will implement the
  draft→spoken-confirm→re-call handshake against this flag.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger("sirious.tools")

NOTES_COLLECTION = "tool_notes"
AUDIT_COLLECTION = "tool_audit"
MAX_NOTE_CHARS = 4000
MAX_SNIPPET_CHARS = 350
MAX_AUDIT_ARG_CHARS = 500


def _truncate(text: Any, limit: int) -> str | None:
    if text is None:
        return None
    s = str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ── Audit log ────────────────────────────────────────────────────────────────

class AuditLog:
    """Fire-and-forget tool-invocation audit trail.

    Firestore mode (default): one doc per invocation in ``tool_audit``.
    Memory mode (null persistence): bounded deque, inspectable in tests/dev.
    """

    def __init__(self, use_firestore: bool = True) -> None:
        self._use_firestore = use_firestore
        self._db: Any = None
        self._ring: collections.deque = collections.deque(maxlen=200)

    def _ensure_db(self) -> Any:
        if self._db is None:
            # Imported lazily so environments without the package (or
            # without ADC) can still import this module; tests patch this.
            from google.cloud.firestore import AsyncClient

            project = os.environ.get("GCP_PROJECT") or os.environ.get(
                "GOOGLE_CLOUD_PROJECT"
            )
            kwargs: dict[str, Any] = {}
            if project:
                kwargs["project"] = project
            self._db = AsyncClient(**kwargs)
        return self._db

    def record(
        self,
        *,
        session_id: str,
        doc_id: str,
        tool: str,
        args: dict[str, Any] | None,
        outcome: str,
        latency_ms: int,
        error: str | None = None,
    ) -> None:
        entry = {
            "ts": time.time(),
            "created_at": _utcnow_iso(),
            "session_id": session_id,
            "session_ref": doc_id,
            "tool": tool,
            "args": {
                k: _truncate(v, MAX_AUDIT_ARG_CHARS)
                for k, v in (args or {}).items()
            },
            "outcome": outcome,
            "latency_ms": latency_ms,
            "error": _truncate(error, MAX_AUDIT_ARG_CHARS),
        }
        if not self._use_firestore:
            self._ring.append(entry)
            return
        # Fire-and-forget: audit must never delay or break the voice path.
        try:
            asyncio.get_running_loop().create_task(self._write(entry))
        except RuntimeError:
            # No running loop (only possible outside the WS hot path,
            # e.g. unit tests): flush synchronously.
            asyncio.run(self._write(entry))

    async def _write(self, entry: dict[str, Any]) -> None:
        try:
            db = self._ensure_db()
            await (
                db.collection(AUDIT_COLLECTION)
                .document(uuid.uuid4().hex)
                .set(entry)
            )
        except Exception:  # noqa: BLE001 — never propagate into the voice path
            log.exception("tool_audit write failed")

    @property
    def entries(self) -> list[dict[str, Any]]:
        """In-memory mode only."""
        return list(self._ring)


# ── Web search providers ─────────────────────────────────────────────────────

class WebSearchProvider:
    """Minimal interface: async search(query) -> list[dict]."""

    async def search(self, query: str) -> list[dict[str, Any]]:  # pragma: nocover
        raise NotImplementedError


class TavilyProvider(WebSearchProvider):
    """Tavily Search API (agent-oriented; free tier 1,000 credits/month)."""

    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str, timeout_s: float = 8.0) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s

    async def search(self, query: str) -> list[dict[str, Any]]:
        import httpx  # transitive dep of google-genai; imported lazily

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(
                self.URL,
                json={
                    "api_key": self._api_key,
                    "query": query,
                    "max_results": 5,
                    "search_depth": "basic",
                    "include_answer": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        results = []
        for r in data.get("results", [])[:5]:
            results.append(
                {
                    "title": r.get("title"),
                    "snippet": _truncate(r.get("content"), MAX_SNIPPET_CHARS),
                    "url": r.get("url"),
                }
            )
        return results


class StaticProvider(WebSearchProvider):
    """Canned results — tests and offline demos."""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results

    async def search(self, query: str) -> list[dict[str, Any]]:
        return self._results


# ── Notes ────────────────────────────────────────────────────────────────────

class NotesStore:
    """Append-only notes in Firestore. Direct await is fine here: tool
    execution already sits outside the audio hot path, and the model cannot
    be answered until the write outcome is known."""

    def __init__(self) -> None:
        self._db: Any = None

    def _ensure_db(self) -> Any:
        if self._db is None:
            # Imported lazily; tests patch this method.
            from google.cloud.firestore import AsyncClient

            project = os.environ.get("GCP_PROJECT") or os.environ.get(
                "GOOGLE_CLOUD_PROJECT"
            )
            kwargs: dict[str, Any] = {}
            if project:
                kwargs["project"] = project
            self._db = AsyncClient(**kwargs)
        return self._db

    async def save(self, *, text: str, topic: str | None, doc_id: str) -> str:
        db = self._ensure_db()
        note_id = uuid.uuid4().hex
        await (
            db.collection(NOTES_COLLECTION)
            .document(note_id)
            .set(
                {
                    "text": text,
                    "topic": topic,
                    "session_ref": doc_id,
                    "created_at": _utcnow_iso(),
                }
            )
        )
        return note_id


class InMemoryNotesStore(NotesStore):
    """Null-persistence stand-in (and test double)."""

    def __init__(self) -> None:
        super().__init__()
        self.saved: list[dict[str, Any]] = []

    async def save(self, *, text: str, topic: str | None, doc_id: str) -> str:
        self._db = object()  # mark configured without touching Firestore
        note_id = uuid.uuid4().hex
        self.saved.append(
            {"id": note_id, "text": text, "topic": topic, "session_ref": doc_id}
        )
        return note_id


# ── Registry ─────────────────────────────────────────────────────────────────

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]          # JSON-schema-ish, translated to types.Schema
    handler: Handler
    requires_confirmation: bool = False


class ToolRegistry:
    """Per-connection tool registry + dispatcher."""

    def __init__(self, *, audit: AuditLog, session_id: str, doc_id: str) -> None:
        self.audit = audit
        self.session_id = session_id
        self.doc_id = doc_id
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self._specs)

    def genai_tools(self) -> list | None:
        """List of types.Tool carrying every registered declaration, or None
        when no tools registered.

        WIRE CONTRACT (learned in prod, revision 00029): LiveConnectConfig
        tools= requires a LIST of types.Tool. A single bare Tool raises
        AttributeError("'tuple' object has no attribute ...") deep inside
        the SDK at connect time.
        """
        if not self._specs:
            return None
        from google.genai import types

        decls = [
            types.FunctionDeclaration(
                name=s.name,
                description=s.description,
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        k: types.Schema(type=v["type"], description=v["description"])
                        for k, v in s.parameters.items()
                    },
                    required=[
                        k
                        for k, v in s.parameters.items()
                        if v.get("required")
                    ],
                ),
            )
            for s in self._specs.values()
        ]
        return [types.Tool(function_declarations=decls)]

    async def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute one tool call. ALWAYS returns a JSON-safe payload dict —
        unknown tools and failures degrade to structured errors, never raise
        into the receive loop. Exactly one audit record per invocation."""
        started = time.monotonic()
        safe_args = {
            k: _truncate(v, MAX_AUDIT_ARG_CHARS)
            for k, v in (args or {}).items()
        }

        def _audit(outcome: str, error: str | None = None) -> None:
            self.audit.record(
                session_id=self.session_id,
                doc_id=self.doc_id,
                tool=name,
                args=safe_args,
                outcome=outcome,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=_truncate(error, MAX_AUDIT_ARG_CHARS),
            )

        spec = self._specs.get(name)
        if spec is None:
            _audit("unknown_tool")
            return {"error": f"unknown tool '{name}'"}

        error: str | None = None
        try:
            payload = await spec.handler(args or {})
            if isinstance(payload, dict) and payload.get("error"):
                # Handler caught its own problem and degraded gracefully.
                error = str(payload["error"])
            return payload
        except Exception as e:  # noqa: BLE001 — degrade gracefully
            log.exception("tool %s failed", name)
            payload = {"error": f"{name} failed"}
            error = repr(e)
            return payload
        finally:
            _audit("error" if error else "ok", error)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ── Handlers ─────────────────────────────────────────────────────────────────

async def _handle_web_search(args: dict[str, Any], provider: WebSearchProvider) -> dict[str, Any]:
    query = str((args or {}).get("query") or "").strip()
    if not query:
        return {"result": "No results", "note": "empty query"}
    try:
        results = await provider.search(query)
    except Exception as e:  # noqa: BLE001 — spoken graceful degradation
        log.warning("web_search provider failure: %r", e)
        return {"error": "web search temporarily unavailable"}
    if not results:
        return {"result": f"No web results found for '{query}'."}
    return {
        "result": f"Found {len(results)} web results for '{query}'.",
        "results": results,
    }


async def _handle_add_note(args: dict[str, Any], notes: NotesStore, doc_id: str) -> dict[str, Any]:
    text = str((args or {}).get("text") or "").strip()
    topic = (args or {}).get("topic")
    if not text:
        return {"error": "empty note"}
    note_id = await notes.save(
        text=_truncate(text, MAX_NOTE_CHARS),
        topic=str(topic) if topic else None,
        doc_id=doc_id,
    )
    return {"result": "Note saved.", "note_id": note_id}


async def _handle_memory_recall(args: dict[str, Any], memory: Any) -> dict[str, Any]:
    """Phase 3 behavior, verbatim: top-5 memories WITH scores (the model
    judges relevance itself)."""
    query = str((args or {}).get("query") or "").strip()
    if not query:
        return {"result": "no results", "note": "empty query"}
    try:
        found = await memory.recall(query, top_k=5)
    except Exception as e:  # noqa: BLE001
        log.warning("memory recall failure: %r", e)
        return {"error": "memory search temporarily unavailable"}
    return {
        "result": (
            "Found these memories about the user's past conversations:"
            if found
            else "No matching memories found."
        ),
        "memories": [
            {
                "text": h.get("text"),
                "type": h.get("type"),
                "score": h.get("score"),
                "date": ((h.get("provenance") or [{}])[-1].get("started_at", "")[:10]) or None,
                "session_ref": (h.get("provenance") or [{}])[-1].get("session_ref"),
            }
            for h in found
        ],
    }


# ── Per-connection builder ───────────────────────────────────────────────────

def build_registry(
    *,
    session_id: str,
    doc_id: str,
    memory: Any,
    persist_enabled: bool | None = None,
    tools_enabled: bool | None = None,
    tavily_key: str | None = None,
) -> tuple[ToolRegistry, AuditLog]:
    """Build this connection's registry according to configuration.

    Returns (registry, audit) — main.py passes registry.genai_tools() into the
    LiveConnectConfig and routes tool_call frames to registry.dispatch().
    """
    if tools_enabled is None:
        tools_enabled = os.environ.get("SIRIOUS_TOOLS") == "1"
    if persist_enabled is None:
        persist_enabled = os.environ.get("SIRIOUS_PERSIST") == "1"
    if tavily_key is None:
        tavily_key = os.environ.get("TAVILY_API_KEY")

    audit = AuditLog(use_firestore=persist_enabled)
    registry = ToolRegistry(audit=audit, session_id=session_id, doc_id=doc_id)

    # Phase 3 agentic recall — unchanged gate (memory store enabled).
    if memory is not None and getattr(memory, "enabled", lambda: False)():
        registry.register(
            ToolSpec(
                name="search_past_conversations",
                description=(
                    "Search the user's past conversations and your long-term "
                    "memories about them. Use whenever the user refers to "
                    "something not covered by what you already know — past "
                    "discussions, facts they told you earlier, plans, people, "
                    "or preferences (e.g. 'did we ever talk about X?', 'what "
                    "did I say about Y?')."
                ),
                parameters={
                    "query": {
                        "type": "STRING",
                        "description": "What to search for, in a few natural words.",
                        "required": True,
                    },
                },
                handler=lambda args: _handle_memory_recall(args, memory),
            )
        )

    if tools_enabled and tavily_key:
        provider = TavilyProvider(tavily_key)
        registry.register(
            ToolSpec(
                name="web_search",
                description=(
                    "Search the public web for current information — news, "
                    "prices, weather, scores, recent events, anything you are "
                    "unsure about or that may have changed recently. Returns "
                    "titles, snippets and links; summarize them verbally."
                ),
                parameters={
                    "query": {
                        "type": "STRING",
                        "description": "The web search query, phrased like a search engine query.",
                        "required": True,
                    },
                },
                handler=lambda args: _handle_web_search(args, provider),
            )
        )

    if tools_enabled and persist_enabled:
        registry.register(
            ToolSpec(
                name="add_note",
                description=(
                    "Save something the user wants kept for later — a quick "
                    "note, an idea, something to remember. Use when the user "
                    "says 'note that…', 'save this…', 'remember to look at…' "
                    "as a deliberate capture request."
                ),
                parameters={
                    "text": {
                        "type": "STRING",
                        "description": "The note content, cleaned up into clear prose.",
                        "required": True,
                    },
                    "topic": {
                        "type": "STRING",
                        "description": "Optional one-or-two-word topic label.",
                    },
                },
                handler=lambda args: _handle_add_note(
                    args,
                    NotesStore() if persist_enabled else InMemoryNotesStore(),
                    doc_id,
                ),
            )
        )

    return registry, audit
