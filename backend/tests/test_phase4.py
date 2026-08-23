"""Phase 4 tests: tool registry, dispatch, audit log, web_search, add_note.

Run from backend/:  .venv/Scripts/python.exe -m pytest tests/ -q
No GCP project, Gemini API, Tavily key, or network needed — Firestore,
httpx, and the genai client are faked out (same pattern as phases 2–3).

Async units follow the phase-3 convention: plain ``asyncio.run()`` inside
sync test functions (no pytest-asyncio dependency).
"""

import asyncio

import pytest
from app import tools as tools_mod
from app.tools import (
    AuditLog,
    InMemoryNotesStore,
    StaticProvider,
    ToolRegistry,
    ToolSpec,
    TavilyProvider,
    WebSearchProvider,
    _handle_add_note,
    _handle_web_search,
    _handle_memory_recall,
    build_registry,
)


# ── Fakes ────────────────────────────────────────────────────────────────────

class _Snap:
    def __init__(self, id, data):
        self.id = id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _DocRef:
    def __init__(self, db, coll, doc_id):
        self.db = db
        self.coll = coll
        self.id = doc_id

    async def set(self, data, merge=False):
        cur = self.db.setdefault(self.coll, {}).setdefault(self.id, {})
        if merge:
            cur.update(data)
        else:
            cur.clear()
            cur.update(data)

    async def get(self):
        return _Snap(self.id, self.db.get(self.coll, {}).get(self.id))

    async def delete(self):
        self.db.get(self.coll, {}).pop(self.id, None)


class _Coll:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def document(self, doc_id):
        return _DocRef(self.db, self.name, doc_id)


class FakeDb:
    """Minimal stand-in for AsyncClient: {collection: {doc_id: data}}."""

    def __init__(self):
        self.db = {}

    def collection(self, name):
        return _Coll(self.db, name)


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeHttpxClient:
    """Stands in for httpx.AsyncClient inside TavilyProvider.search."""
    captured = {}
    next_response = _FakeResponse({"results": []})

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        FakeHttpxClient.captured = {"url": url, "body": json}
        return FakeHttpxClient.next_response


# ── Registry construction / gating ──────────────────────────────────────────

class _EnabledMemory:
    def enabled(self):
        return True


class _DisabledMemory:
    def enabled(self):
        return False


def _mk_registry(**kw):
    kw.setdefault("session_id", "sess-1")
    kw.setdefault("doc_id", "doc-1")
    return build_registry(**kw)


def test_no_tools_when_gates_off(monkeypatch):
    for var in ("SIRIOUS_TOOLS", "TAVILY_API_KEY", "SIRIOUS_PERSIST"):
        monkeypatch.delenv(var, raising=False)
    reg, audit = _mk_registry(memory=_DisabledMemory())
    assert reg.names() == []
    assert reg.genai_tool() is None  # Gemini rejects empty declarations


def test_memory_tool_only_without_tools_gate(monkeypatch):
    monkeypatch.delenv("SIRIOUS_TOOLS", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    reg, _ = _mk_registry(memory=_EnabledMemory())
    # Phase 3 behavior preserved: recall works without SIRIOUS_TOOLS.
    assert reg.names() == ["search_past_conversations"]


def test_phase4_tools_register_with_gate(monkeypatch):
    monkeypatch.setenv("SIRIOUS_TOOLS", "1")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    monkeypatch.setenv("SIRIOUS_PERSIST", "1")
    reg, _ = _mk_registry(memory=_EnabledMemory())
    assert reg.names() == [
        "add_note",
        "search_past_conversations",
        "web_search",
    ]


def test_web_search_needs_key_but_note_needs_only_persist(monkeypatch):
    monkeypatch.setenv("SIRIOUS_TOOLS", "1")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SIRIOUS_PERSIST", "1")
    reg, _ = _mk_registry(memory=_DisabledMemory())
    assert reg.names() == ["add_note"]


def test_genai_tool_declares_all_specs():
    reg = ToolRegistry(
        audit=AuditLog(use_firestore=False),
        session_id="s",
        doc_id="d",
    )
    reg.register(
        ToolSpec(
            name="t_one",
            description="one",
            parameters={
                "q": {"type": "STRING", "description": "query", "required": True},
                "opt": {"type": "STRING", "description": "optional"},
            },
            handler=lambda a: {},
        )
    )
    tool = reg.genai_tool()
    assert tool is not None
    decl = tool.function_declarations[0]
    assert decl.name == "t_one"
    assert set(decl.parameters.properties) == {"q", "opt"}
    assert decl.parameters.required == ["q"]


# ── Dispatch semantics ───────────────────────────────────────────────────────

def test_dispatch_ok_audits_once():
    audit = AuditLog(use_firestore=False)
    reg = ToolRegistry(audit=audit, session_id="s", doc_id="d")

    async def handler(args):
        return {"result": "fine"}

    reg.register(ToolSpec(name="t", description="", parameters={}, handler=handler))

    payload = asyncio.run(reg.dispatch("t", {"q": "hello"}))
    assert payload == {"result": "fine"}
    entries = audit.entries
    assert len(entries) == 1
    assert entries[0]["outcome"] == "ok"
    assert entries[0]["args"] == {"q": "hello"}
    assert entries[0]["latency_ms"] >= 0


def test_dispatch_unknown_tool():
    audit = AuditLog(use_firestore=False)
    reg = ToolRegistry(audit=audit, session_id="s", doc_id="d")
    payload = asyncio.run(reg.dispatch("nope", {}))
    assert "unknown tool" in payload["error"]
    assert audit.entries[0]["outcome"] == "unknown_tool"


def test_dispatch_handler_exception_degrades():
    audit = AuditLog(use_firestore=False)
    reg = ToolRegistry(audit=audit, session_id="s", doc_id="d")

    async def boom(args):
        raise ValueError("kaboom")

    reg.register(ToolSpec(name="t", description="", parameters={}, handler=boom))
    payload = asyncio.run(reg.dispatch("t", {}))
    assert payload == {"error": "t failed"}
    assert audit.entries[0]["outcome"] == "error"
    assert "kaboom" in audit.entries[0]["error"]


def test_audit_args_truncated():
    audit = AuditLog(use_firestore=False)
    reg = ToolRegistry(audit=audit, session_id="s", doc_id="d")

    async def handler(args):
        return {"result": "ok"}

    reg.register(ToolSpec(name="t", description="", parameters={}, handler=handler))
    asyncio.run(reg.dispatch("t", {"text": "x" * 5000}))
    stored = audit.entries[0]["args"]["text"]
    assert len(stored) < 520  # truncated, not the raw 5000


# ── web_search ───────────────────────────────────────────────────────────────

def test_web_search_static_provider_payload():
    provider = StaticProvider(
        [{"title": "A", "snippet": "sa", "url": "u1"},
         {"title": "B", "snippet": "sb", "url": "u2"}]
    )
    payload = asyncio.run(_handle_web_search({"query": "india rail news"}, provider))
    assert payload["result"].startswith("Found 2 web results")
    assert [r["url"] for r in payload["results"]] == ["u1", "u2"]


def test_web_search_empty_query():
    payload = asyncio.run(_handle_web_search({"query": "   "}, StaticProvider([])))
    assert payload == {"result": "No results", "note": "empty query"}


def test_web_search_provider_failure_is_graceful():
    class Exploding(WebSearchProvider):
        async def search(self, query):
            raise RuntimeError("network down")

    payload = asyncio.run(_handle_web_search({"query": "q"}, Exploding()))
    assert payload == {"error": "web search temporarily unavailable"}


def test_web_search_no_results_reads_well():
    payload = asyncio.run(_handle_web_search({"query": "zzz"}, StaticProvider([])))
    assert "No web results found" in payload["result"]


def test_tavily_request_shape_and_mapping(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeHttpxClient)
    FakeHttpxClient.next_response = _FakeResponse(
        {
            "results": [
                {
                    "title": "Result",
                    "content": "c" * 1000,  # long → snippet truncated
                    "url": "https://example.com/r",
                }
            ]
        }
    )
    FakeHttpxClient.captured = {}
    provider = TavilyProvider("tvly-test-key")
    results = asyncio.run(provider.search("best coffee grinder"))
    body = FakeHttpxClient.captured["body"]
    assert FakeHttpxClient.captured["url"] == TavilyProvider.URL
    assert body["api_key"] == "tvly-test-key"
    assert body["query"] == "best coffee grinder"
    assert body["max_results"] == 5
    assert len(results) == 1
    assert results[0]["title"] == "Result"
    assert results[0]["url"] == "https://example.com/r"
    assert len(results[0]["snippet"]) <= tools_mod.MAX_SNIPPET_CHARS


# ── add_note ────────────────────────────────────────────────────────────────

def test_add_note_saves_and_returns_id():
    notes = InMemoryNotesStore()
    payload = asyncio.run(
        _handle_add_note({"text": "buy milk", "topic": "errands"}, notes, "doc-9")
    )
    assert payload["result"] == "Note saved."
    assert payload["note_id"]
    saved = notes.saved[0]
    assert saved["text"] == "buy milk"
    assert saved["topic"] == "errands"
    assert saved["session_ref"] == "doc-9"


def test_add_note_empty_text_rejected():
    payload = asyncio.run(_handle_add_note({"text": ""}, InMemoryNotesStore(), "d"))
    assert payload == {"error": "empty note"}


def test_notes_store_firestore_mode():
    fake_db = FakeDb()
    store = tools_mod.NotesStore()
    store._ensure_db = lambda: fake_db
    note_id = asyncio.run(store.save(text="t", topic=None, doc_id="doc-1"))
    written = fake_db.db[tools_mod.NOTES_COLLECTION][note_id]
    assert written["text"] == "t"
    assert written["session_ref"] == "doc-1"
    assert written["created_at"]


# ── AuditLog Firestore mode ─────────────────────────────────────────────────

def test_audit_firestore_write():
    fake_db = FakeDb()
    audit = AuditLog(use_firestore=True)
    audit._ensure_db = lambda: fake_db
    audit.record(
        session_id="ws-sess",
        doc_id="conv-1",
        tool="web_search",
        args={"query": "weather mumbai"},
        outcome="ok",
        latency_ms=420,
    )  # no running loop here → synchronous flush path
    docs = fake_db.db[tools_mod.AUDIT_COLLECTION]
    assert len(docs) == 1
    entry = next(iter(docs.values()))
    assert entry["tool"] == "web_search"
    assert entry["outcome"] == "ok"
    assert entry["session_ref"] == "conv-1"
    assert entry["latency_ms"] == 420


# ── Phase 3 recall handler parity ────────────────────────────────────────────

def test_memory_recall_shape_unchanged():
    class FakeMemory:
        async def recall(self, query, top_k=5):
            assert top_k == 5
            return [
                {
                    "text": "User discussed peacocks",
                    "type": "episodic",
                    "score": 0.91,
                    "provenance": [
                        {
                            "started_at": "2026-08-22T10:00:00+00:00",
                            "session_ref": "conv-peacock",
                        }
                    ],
                }
            ]

    payload = asyncio.run(_handle_memory_recall({"query": "birds"}, FakeMemory()))
    assert payload["result"].startswith("Found these memories")
    mem = payload["memories"][0]
    assert mem["date"] == "2026-08-22"
    assert mem["session_ref"] == "conv-peacock"
    assert mem["score"] == 0.91


def test_memory_recall_failure_graceful():
    class BrokenMemory:
        async def recall(self, query, top_k=5):
            raise RuntimeError("down")

    payload = asyncio.run(_handle_memory_recall({"query": "x"}, BrokenMemory()))
    assert payload == {"error": "memory search temporarily unavailable"}


# ── main.py integration surface ──────────────────────────────────────────────

def test_main_imports_registry_builder():
    from app import main as main_mod

    assert hasattr(main_mod, "build_registry")


def test_end_to_end_registry_dispatch_with_fakes(monkeypatch):
    """The exact path the receive loop takes: build per config → dispatch."""
    monkeypatch.setenv("SIRIOUS_TOOLS", "1")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    monkeypatch.delenv("SIRIOUS_PERSIST", raising=False)
    import httpx as _hx

    monkeypatch.setattr(_hx, "AsyncClient", FakeHttpxClient)
    FakeHttpxClient.next_response = _FakeResponse(
        {"results": [{"title": "N", "content": "c", "url": "https://n.co"}]}
    )

    reg, audit = _mk_registry(memory=_EnabledMemory())
    assert reg.names() == ["search_past_conversations", "web_search"]

    payload = asyncio.run(reg.dispatch("web_search", {"query": "news"}))
    assert payload["results"][0]["url"] == "https://n.co"

    outcomes = [(e["tool"], e["outcome"]) for e in audit.entries]
    assert ("web_search", "ok") in outcomes
