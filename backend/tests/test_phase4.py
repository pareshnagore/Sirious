"""Phase 4 tests: tool registry, dispatch, audit log, web_search, add_note.

Run from backend/:  .venv/Scripts/python.exe -m pytest tests/ -q
No GCP project, Gemini API, Tavily key, or network needed — Firestore,
httpx, and the genai client are faked out (same pattern as phases 2–3).

Async units follow the phase-3 convention: plain ``asyncio.run()`` inside
sync test functions (no pytest-asyncio dependency).
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from app import tools as tools_mod
from app.tools import (
    AuditLog,
    InMemoryNotesStore,
    InMemoryReminderStore,
    ReminderStore,
    StaticProvider,
    ToolRegistry,
    ToolSpec,
    TavilyProvider,
    WebSearchProvider,
    _handle_add_note,
    _handle_web_search,
    _handle_memory_recall,
    _handle_cancel_reminder,
    _handle_confirm_reminder,
    _handle_create_reminder,
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
    assert reg.genai_tools() is None  # empty declarations are never sent


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


def test_genai_tools_wire_shape_is_list():
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
    tools = reg.genai_tools()
    assert isinstance(tools, list) and len(tools) == 1
    # WIRE CONTRACT (prod, rev 00029): LiveConnectConfig tools= iterates the
    # value; a bare single types.Tool here dies at connect with
    # AttributeError("'tuple' object has no attribute 'function_declarations'").
    decl = tools[0].function_declarations[0]
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


# ── Reminders: parsing / validation ─────────────────────────────────────────

NOW_S = 1_800_000_000.0  # fixed "now" so validation windows are deterministic
IST = timezone(timedelta(hours=5, minutes=30))


def _due(hours: float) -> str:
    return datetime.fromtimestamp(
        NOW_S + hours * 3600, tz=timezone.utc
    ).isoformat()


class TestParseDueAt:
    def test_iso_with_offset_accepted(self):
        ts, err = tools_mod._parse_due_at(_due(24), now_s=NOW_S)
        assert err is None
        assert abs(ts - (NOW_S + 24 * 3600)) < 1

    def test_z_suffix_accepted(self):
        raw = _due(2).replace("+00:00", "Z")
        ts, err = tools_mod._parse_due_at(raw, now_s=NOW_S)
        assert err is None and ts > NOW_S

    def test_naive_iso_interpreted_in_user_tz(self):
        # Naive stamp = user's local wall clock (Asia/Kolkata), NOT UTC.
        # NOW_S is Fri 2027-01-15 10:00 IST, so 23:30 same day is ~13.5h out.
        naive = "2027-01-15T23:30:00"
        ts, err = tools_mod._parse_due_at(naive, now_s=NOW_S)
        assert err is None
        assert datetime.fromtimestamp(
            ts, tz=IST
        ).strftime("%Y-%m-%d %H:%M") == "2027-01-15 23:30"

    def test_naive_iso_in_the_past_still_rejected(self):
        ts, err = tools_mod._parse_due_at("2027-01-15T09:00:00", now_s=NOW_S)
        assert ts is None and "future" in err

    def test_garbage_rejected(self):
        ts, err = tools_mod._parse_due_at("friday-ish", now_s=NOW_S)
        assert ts is None and "could not parse" in err

    def test_past_rejected(self):
        ts, err = tools_mod._parse_due_at(_due(-1), now_s=NOW_S)
        assert ts is None and "future" in err

    def test_within_min_lead_rejected(self):
        soon = datetime.fromtimestamp(NOW_S + 90, tz=timezone.utc).isoformat()
        ts, err = tools_mod._parse_due_at(soon, now_s=NOW_S)
        assert ts is None and "future" in err

    def test_just_over_min_lead_allowed(self):
        ok = datetime.fromtimestamp(NOW_S + 121, tz=timezone.utc).isoformat()
        ts, err = tools_mod._parse_due_at(ok, now_s=NOW_S)
        assert err is None and ts > NOW_S

    def test_beyond_horizon_rejected(self):
        ts, err = tools_mod._parse_due_at(_due(24 * 400), now_s=NOW_S)
        assert ts is None and "days away" in err


# ── Natural-language when resolution ────────────────────────────────────────
#
# NOW_S = 1_800_000_000 → Fri 15 Jan 2027 10:00 IST (UTC+05:30).
# All expectations below are anchored to that.

def _resolve(text):
    """Resolve at fixed now; return (utc_ts | None, err | None)."""
    return tools_mod._resolve_due(text, now_s=NOW_S)


def _ist(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=IST).timestamp()


class TestResolveWhen:
    def test_in_n_minutes(self):
        ts, err = _resolve("in 20 minutes")
        assert err is None
        assert abs(ts - (NOW_S + 20 * 60)) < 2

    def test_in_word_number_hours(self):
        ts, err = _resolve("in five hours")
        assert err is None
        assert abs(ts - (NOW_S + 5 * 3600)) < 2

    def test_tomorrow_morning_is_9am(self):
        ts, err = _resolve("tomorrow morning")
        assert err is None
        assert abs(ts - _ist(2027, 1, 16, 9, 0)) < 1

    def test_friday_bare_is_this_week(self):
        ts, err = _resolve("friday morning")
        assert err is None
        assert abs(ts - _ist(2027, 1, 22, 9, 0)) < 1  # next Friday after Sat

    def test_next_weekday_adds_a_week(self):
        ts, err = _resolve("next monday 2 pm")
        assert err is None
        assert abs(ts - _ist(2027, 1, 25, 14, 0)) < 1

    def test_day_after_tomorrow_explicit_time(self):
        ts, err = _resolve("day after tomorrow at 5 pm")
        assert err is None
        # NOW_S = Fri 15 Jan → day after tomorrow = Sun 17 Jan.
        assert abs(ts - _ist(2027, 1, 17, 17, 0)) < 1

    def test_tonight_default(self):
        ts, err = _resolve("tonight")
        assert err is None
        assert abs(ts - _ist(2027, 1, 15, 21, 0)) < 1

    def test_evening_word_shifts_pm(self):
        ts, err = _resolve("tomorrow evening")
        assert err is None
        assert abs(ts - _ist(2027, 1, 16, 19, 0)) < 1

    def test_bare_clock_past_rolls_to_tomorrow(self):
        ts, err = _resolve("at 9 am")   # said at 10:00 → tomorrow 09:00
        assert err is None
        assert abs(ts - _ist(2027, 1, 16, 9, 0)) < 1

    def test_half_hours_supported(self):
        ts, err = _resolve("tomorrow at 9:30 am")
        assert err is None
        assert abs(ts - _ist(2027, 1, 16, 9, 30)) < 1

    def test_unrecognised_phrase_errors_with_examples(self):
        ts, err = _resolve("sometime next spring maybe")
        assert ts is None
        assert "could not understand when" in err

    def test_relative_below_min_lead_still_rejected_by_window(self):
        ts, err = _resolve("in a minute")
        assert ts is None and "future" in err

    def test_iso_still_works_through_resolver(self):
        ts, err = _resolve(_due(48))
        assert err is None
        assert abs(ts - (NOW_S + 48 * 3600)) < 1


# ── get_current_time ─────────────────────────────────────────────────────────

def test_get_current_time_shape(monkeypatch):
    monkeypatch.setenv("SIRIOUS_TZ", "Asia/Kolkata")
    payload = asyncio.run(tools_mod._handle_get_current_time())
    assert payload["result"].startswith("It is ")
    assert any(
        wd in payload["result"]
        for wd in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                   "Saturday", "Sunday")
    )
    assert payload["weekday"] in (
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        "Saturday", "Sunday",
    )
    assert "T" in payload["iso_utc"]


# ── Reminders: handlers against the in-memory store ──────────────────────────

def test_create_reminder_happy_path():
    store = InMemoryReminderStore()
    payload = asyncio.run(
        _handle_create_reminder(
            {"text": "call Raj", "due_at": _due(48)}, store, "doc-7",
            now_s=NOW_S,
        )
    )
    assert payload["result"].startswith("Draft ready")
    assert payload["next_step"] == "confirm_reminder"
    draft_id = payload["draft_id"]
    data = store.reminders[draft_id]
    assert data["text"] == "call Raj"
    assert data["status"] == "draft"
    assert data["session_ref"] == "doc-7"


def test_create_reminder_empty_text():
    payload = asyncio.run(
        _handle_create_reminder(
            {"text": "  ", "due_at": _due(2)}, InMemoryReminderStore(), "d",
            now_s=NOW_S,
        )
    )
    assert payload == {"error": "empty reminder text"}


def test_create_reminder_bad_due_at_is_structured_error():
    payload = asyncio.run(
        _handle_create_reminder(
            {"text": "call Raj", "due_at": "next sometime"},
            InMemoryReminderStore(), "d", now_s=NOW_S,
        )
    )
    # Unparsable input now falls through to NL resolution; the error must
    # still be structured and tell the model which forms ARE supported.
    assert payload["error"].startswith("could not understand when")
    assert "in 20 minutes" in payload["error"]


def test_confirm_lifecycle_draft_to_scheduled():
    store = InMemoryReminderStore()
    draft_id = asyncio.run(
        _handle_create_reminder(
            {"text": "pay bill", "due_at": _due(24)}, store, "doc-1",
            now_s=NOW_S,
        )
    )["draft_id"]
    out = asyncio.run(
        _handle_confirm_reminder({"reminder_id": draft_id}, store, now_s=NOW_S)
    )
    assert out["result"] == "Reminder confirmed."
    assert store.reminders[draft_id]["status"] == "scheduled"


def test_confirm_unknown_id():
    out = asyncio.run(
        _handle_confirm_reminder(
            {"reminder_id": "ghost"}, InMemoryReminderStore(), now_s=NOW_S
        )
    )
    assert "unknown reminder" in out["error"]


def test_double_confirm_is_idempotent():
    store = InMemoryReminderStore()
    draft_id = asyncio.run(
        _handle_create_reminder(
            {"text": "x", "due_at": _due(5)}, store, "d", now_s=NOW_S
        )
    )["draft_id"]
    asyncio.run(_handle_confirm_reminder({"reminder_id": draft_id}, store, now_s=NOW_S))
    again = asyncio.run(
        _handle_confirm_reminder({"reminder_id": draft_id}, store, now_s=NOW_S + 10)
    )
    assert again.get("already_confirmed") is True


def test_stale_draft_cannot_be_confirmed():
    """A draft older than DRAFT_TTL_S must fail closed: what the user just
    said 'yes' to may no longer match the stored draft."""
    store = InMemoryReminderStore()
    draft_id = asyncio.run(
        _handle_create_reminder(
            {"text": "old one", "due_at": _due(24)}, store, "d", now_s=NOW_S
        )
    )["draft_id"]
    later = NOW_S + tools_mod.DRAFT_TTL_S + 1
    out = asyncio.run(
        _handle_confirm_reminder({"reminder_id": draft_id}, store, now_s=later)
    )
    assert "expired" in out["error"]
    assert store.reminders[draft_id]["status"] == "draft"  # unchanged


def test_cancel_lifecycle_and_already_closed():
    store = InMemoryReminderStore()
    draft_id = asyncio.run(
        _handle_create_reminder(
            {"text": "x", "due_at": _due(6)}, store, "d", now_s=NOW_S
        )
    )["draft_id"]
    out = asyncio.run(_handle_cancel_reminder({"reminder_id": draft_id}, store))
    assert out["result"] == "Reminder cancelled."
    assert store.reminders[draft_id]["status"] == "cancelled"
    again = asyncio.run(_handle_cancel_reminder({"reminder_id": draft_id}, store))
    assert again["result"] == "Reminder was already closed."


# ── Reminders: Firestore-mode store ──────────────────────────────────────────

def test_reminder_store_firestore_roundtrip():
    fake_db = FakeDb()
    store = ReminderStore()
    store._ensure_db = lambda: fake_db
    rid = asyncio.run(
        store.create_draft(
            text="t", due_ts=NOW_S, due_iso=_due(1), doc_id="conv-1",
            created_at="2026-08-23T10:00:00+00:00",
        )
    )
    doc = fake_db.db[tools_mod.REMINDERS_COLLECTION][rid]
    assert doc["status"] == "draft"
    assert doc["session_ref"] == "conv-1"

    status = asyncio.run(store.get_status(rid))
    assert status["text"] == "t"

    asyncio.run(store.set_status(rid, "scheduled"))
    assert fake_db.db[tools_mod.REMINDERS_COLLECTION][rid]["status"] == "scheduled"


# ── Reminders: gating ────────────────────────────────────────────────────────

def test_reminder_tools_hidden_without_gate(monkeypatch):
    monkeypatch.setenv("SIRIOUS_TOOLS", "1")
    monkeypatch.setenv("SIRIOUS_PERSIST", "1")
    monkeypatch.delenv("SIRIOUS_REMINDERS", raising=False)
    reg, _ = _mk_registry(memory=_DisabledMemory())
    assert not any(n.startswith(("create_", "confirm_", "cancel_")) for n in reg.names())


def test_reminder_tools_register_with_gate(monkeypatch):
    monkeypatch.setenv("SIRIOUS_TOOLS", "1")
    monkeypatch.delenv("SIRIOUS_PERSIST", raising=False)  # null mode → memory store
    monkeypatch.setenv("SIRIOUS_REMINDERS", "1")
    reg, audit = _mk_registry(memory=_DisabledMemory())
    for name in ("create_reminder", "confirm_reminder", "cancel_reminder"):
        assert name in reg.names()

    # Full flow through the REAL dispatcher (audited exactly once per call).
    future = _due(24)
    created = asyncio.run(
        reg.dispatch("create_reminder", {"text": "call Raj", "due_at": future})
    )
    assert created["next_step"] == "confirm_reminder"
    confirmed = asyncio.run(
        reg.dispatch("confirm_reminder", {"reminder_id": created["draft_id"]})
    )
    assert confirmed["result"] == "Reminder confirmed."
    outcomes = [e["outcome"] for e in audit.entries]
    assert outcomes.count("ok") == 2


def test_reminder_null_mode_uses_inmemory_store(monkeypatch):
    monkeypatch.setenv("SIRIOUS_TOOLS", "1")
    monkeypatch.delenv("SIRIOUS_PERSIST", raising=False)
    monkeypatch.setenv("SIRIOUS_REMINDERS", "1")
    reg, _ = _mk_registry(memory=_DisabledMemory())
    spec = reg._specs["create_reminder"]
    stores = [f.cell_contents for f in (spec.handler.__closure__ or [])]
    assert any(isinstance(s, InMemoryReminderStore) for s in stores)


def test_reminder_persist_mode_uses_firestore_store(monkeypatch):
    monkeypatch.setenv("SIRIOUS_TOOLS", "1")
    monkeypatch.setenv("SIRIOUS_PERSIST", "1")
    monkeypatch.setenv("SIRIOUS_REMINDERS", "1")
    reg, _ = _mk_registry(memory=_DisabledMemory())
    for name in ("create_reminder", "confirm_reminder", "cancel_reminder"):
        stores = [
            f.cell_contents
            for f in (reg._specs[name].handler.__closure__ or [])
        ]
        assert any(isinstance(s, ReminderStore) and not isinstance(
            s, InMemoryReminderStore) for s in stores), name


# ── main.py wiring ───────────────────────────────────────────────────────────

def test_main_imports_registry_builder():
    from app import main as main_mod

    assert hasattr(main_mod, "build_registry")


def test_main_has_no_prompt_time_injection():
    """Temporal grounding must stay OUT of system_instruction: a per-session
    timestamp churns the prompt prefix on every reconnect and goes stale on
    long/resumed sessions. The clock lives in get_current_time instead."""
    import app.main as main_mod
    import inspect

    src = inspect.getsource(main_mod)
    assert "time_context" not in src
    assert "Current date and time" not in src
