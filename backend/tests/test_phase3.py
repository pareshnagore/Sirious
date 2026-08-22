"""Phase 3 tests: memory extraction, dedup, retrieval, REST controls.

Run from backend/:  .venv/Scripts/python.exe -m pytest tests/ -q
No GCP project, Gemini API, or network needed — Firestore AND the genai
client are faked out.

Pattern notes
-------------
- MemoryStore hot-path methods enqueue onto ONE writer task created inside
  asyncio.run(); drain via ``await store._queue.join()`` after each op.
- The fake genai client lets tests inject canned extraction JSON and
  controlled embedding vectors keyed by exact text.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import memory as memory_mod
from app.memory import (
    MemoryStore,
    NullMemoryStore,
    _cosine,
    get_memory_store,
    reset_memory_store_for_tests,
)
from app import main as main_mod


# ── Fakes ────────────────────────────────────────────────────────────────────

class _Snap:
    def __init__(self, id, data, ref=None):
        self.id = id
        self._data = data
        self.exists = data is not None
        self.reference = ref  # real Firestore snapshots carry their doc ref

    def to_dict(self):
        return self._data

class FakeDocRef:
    def __init__(self, db, coll, doc_id):
        self.db = db
        self.coll = coll
        self.id = doc_id

    async def get(self):
        return _Snap(self.id, self.db.docs.get(self.coll, {}).get(self.id))

    async def set(self, data, merge=False):
        cur = self.db.docs.setdefault(self.coll, {}).setdefault(self.id, {})
        if merge:
            cur.update(data)
        else:
            cur.clear()
            cur.update(data)

    async def delete(self):
        self.db.docs.get(self.coll, {}).pop(self.id, None)


class FakeQuery:
    def __init__(self, db, coll, field=None, reverse=True):
        self.db = db
        self.coll = coll
        self.field = field or "created_ms"
        self.reverse = reverse
        self._limit = 100

    def order_by(self, field, direction="ASCENDING"):
        self.field = field
        self.reverse = direction == "DESCENDING"
        return self

    def limit(self, n):
        self._limit = n
        return self

    async def stream(self):
        items = sorted(
            self.db.docs.get(self.coll, {}).items(),
            key=lambda kv: kv[1].get(self.field, 0),
            reverse=self.reverse,
        )[: self._limit]
        for doc_id, data in items:
            yield _Snap(
                doc_id, data,
                ref=self.db.collection(self.coll).document(doc_id),
            )


class FakeCollection:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def document(self, doc_id):
        return FakeDocRef(self.db, self.name, doc_id)

    async def add(self, data):
        doc_id = f"gen-{len(self.db.docs.get(self.name, {})) + 1}"
        self.db.docs.setdefault(self.name, {})[doc_id] = dict(data)
        return None, doc_id

    def order_by(self, field, direction="DESCENDING"):
        return FakeQuery(self.db, self.name, field, direction == "DESCENDING")


class FakeDB:
    """Covers collections used by memory.py + the sessions read."""

    def __init__(self):
        self.docs = {}

    def collection(self, name):
        if name not in getattr(self, "_coll_cache", {}):
            self._coll_cache = getattr(self, "_coll_cache", {})
            self._coll_cache[name] = FakeCollection(self, name)
        return self._coll_cache[name]


class FakeGenAI:
    """generate_content → canned parsed JSON; embed_content → dict lookup."""

    def __init__(self, extract_result=None, fail_extract=False):
        self.extract_result = extract_result or {"memories": []}
        self.fail_extract = fail_extract
        self.embeddings_by_text: dict[str, list[float]] = {}
        self.calls = {"extract": 0, "embed": 0}
        self.aio = self
        self.models = self

    async def generate_content(self, **kwargs):
        self.calls["extract"] += 1
        if self.fail_extract:
            raise RuntimeError("gemini down")
        r = type("R", (), {})()
        r.parsed = self.extract_result
        return r

    def embed_content(self, **kwargs):
        self.calls["embed"] += 1
        texts = kwargs["contents"]
        if isinstance(texts, str):
            texts = [texts]

        def default_vec(t: str) -> list[float]:
            # Deterministic per-text fallback so tests don't need to seed
            # every embedded string; distinct texts stay dissimilar enough
            # to avoid accidental dedup.
            return [float(len(t) % 7), float(sum(map(ord, t)) % 5), 1.0]

        class E:
            def __init__(self, v):
                self.values = v

        class R:
            def __init__(self, vals):
                self.embeddings = [E(v) for v in vals]

        return R([
            self.embeddings_by_text.get(t) or default_vec(t)
            for t in texts
        ])


@pytest.fixture
def make_memory(monkeypatch):
    """Factory: (store, db, genai) fully faked."""
    db = FakeDB()
    genai = FakeGenAI()

    def factory(extract_result=None, fail_extract=False):
        g = FakeGenAI(extract_result, fail_extract)
        s = MemoryStore()
        monkeypatch.setattr(s, "_ensure_db", lambda: db)
        monkeypatch.setattr(s, "_ensure_genai", lambda: g)
        return s, db, g

    # default pair for tests that build their own later
    return factory, db, genai


async def _drain(store):
    if store._queue is not None:
        await store._queue.join()


def _seed_session(db, doc_id="cs-1", turns=None, updated_ms=100):
    db.docs.setdefault("sessions", {})[doc_id] = {
        "title": "What is the color of a peacock?",
        "started_at": "2026-08-22T10:00:00+00:00",
        "updated_ms": updated_ms,
        "turns": turns
        or [
            {
                "id": "t1",
                "user_text": "What is the color of a peacock?",
                "assistant_text": "A peacock is vivid blue and green.",
            },
            {
                "id": "t2",
                "user_text": "I have an interview at Acme next Tuesday",
                "assistant_text": "Good luck — prepare your Acme project stories.",
            },
        ],
    }


def _snapshot(turns=None):
    """Handler-style turn snapshot (what store.snapshot_turns returns)."""
    return turns or [
        {"id": "t1",
         "user_text": "What is the color of a peacock?",
         "assistant_text": "A peacock is vivid blue and green."},
        {"id": "t2",
         "user_text": "I have an interview at Acme next Tuesday",
         "assistant_text": "Good luck — prepare your Acme project stories."},
    ]


EXTRACT_OK = {
    "memories": [
        {
            "type": "episodic",
            "text": "User and assistant discussed peacock colors",
            "topics": ["birds", "peacocks", "colors"],
            "entities": [],
            "turn_refs": [1],
        },
        {
            "type": "semantic",
            "text": "User has an interview at Acme next Tuesday",
            "topics": ["career"],
            "entities": ["Acme"],
            "turn_refs": [2],
        },
    ]
}


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_cosine():
    assert _cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert _cosine([], []) == 0.0
    assert _cosine([0, 0], [1, 1]) == 0.0


# ── Extraction pipeline ──────────────────────────────────────────────────────

def test_extraction_writes_memories_with_provenance(make_memory):
    factory, _, _ = make_memory
    s, db, g = factory(extract_result=EXTRACT_OK)

    async def main():
        s.request_extraction("cs-1", _snapshot())
        await _drain(s)

    asyncio.run(main())

    mems = list(db.docs.get("memories", {}).values())
    assert len(mems) == 2
    episodic = next(m for m in mems if m["type"] == "episodic")
    semantic = next(m for m in mems if m["type"] == "semantic")

    assert episodic["topics"] == ["birds", "peacocks", "colors"]
    assert episodic["provenance"][0]["session_ref"] == "cs-1"
    assert episodic["provenance"][0]["turn_ids"] == ["t1"]
    assert semantic["provenance"][0]["turn_ids"] == ["t2"]
    assert all(m["speaker"] is None for m in mems)      # multi-user later
    assert all(m["deleted"] is False for m in mems)
    assert all(len(m["embedding"]) == 3 for m in mems)  # fake embeds

    meta = db.docs.get("memory_meta", {})["cs-1"]
    assert set(meta["extracted_turn_ids"]) == {"t1", "t2"}


def test_extraction_watermark_skips_already_extracted(make_memory):
    factory, _, _ = make_memory
    s, db, g = factory(extract_result=EXTRACT_OK)
    db.docs.setdefault("memory_meta", {})["cs-1"] = {
        "extracted_turn_ids": ["t1", "t2"]
    }

    async def main():
        s.request_extraction("cs-1", _snapshot())   # same turns again
        await _drain(s)

    asyncio.run(main())
    assert g.calls["extract"] == 0          # nothing new → no model call
    assert db.docs.get("memories", {}) == {}


def test_failed_extraction_keeps_watermark_and_writer_alive(make_memory):
    factory, _, _ = make_memory
    s, db, g = factory(fail_extract=True)

    async def main():
        s.request_extraction("cs-1", _snapshot())
        await _drain(s)                      # failure swallowed by writer
        assert db.docs.get("memory_meta", {}).get("cs-1") is None
        g.fail_extract = False
        g.extract_result = EXTRACT_OK
        s.request_extraction("cs-1", _snapshot())
        await _drain(s)

    asyncio.run(main())
    assert len(db.docs.get("memories", {})) == 2          # retried OK
    assert set(db.docs["memory_meta"]["cs-1"]["extracted_turn_ids"]) == {"t1", "t2"}


def test_resume_snapshot_reprocesses_only_new_turns(make_memory):
    """Resumed session: handler snapshot holds ALL conversation turns; the
    turn-ID watermark must re-extract only the tail (count-based would skip)."""
    factory, _, _ = make_memory
    s, db, g = factory(extract_result={"memories": [
        {"type": "semantic", "text": f"fact from turn {n}",
         "turn_refs": [n]} for n in (1,)
    ]})

    async def main():
        # Segment 1: turns t1 only.
        g.embeddings_by_text = {
            "fact from turn 1": [1.0, 0.0],
            "fact from turn 2": [0.0, 1.0],   # orthogonal → must NOT dedup
        }
        s.request_extraction("cs-1", _snapshot()[:1])
        await _drain(s)
        assert set(db.docs["memory_meta"]["cs-1"]["extracted_turn_ids"]) == {"t1"}

        # Segment 2 (resumed): snapshot now has t1+t2(+t3); extractor sees t2.
        g.extract_result = {"memories": [
            {"type": "semantic", "text": "fact from turn 2", "turn_refs": [1]}
        ]}
        s.request_extraction(
            "cs-1",
            _snapshot([
                *_snapshot()[:2],
                {"id": "t3", "user_text": "bye", "assistant_text": "bye!"},
            ]),
        )
        await _drain(s)

    asyncio.run(main())

    texts = sorted(m["text"] for m in db.docs.get("memories", {}).values())
    assert texts == ["fact from turn 1", "fact from turn 2"]
    # Segment 1 marked t1; segment 2's snapshot marks every not-yet-done
    # id it carried (t2, t3) — even though only t2 produced a memory.
    ids = set(db.docs["memory_meta"]["cs-1"]["extracted_turn_ids"])
    assert ids == {"t1", "t2", "t3"}


def test_empty_snapshot_is_noop(make_memory):
    factory, _, _ = make_memory
    s, db, g = factory(extract_result=EXTRACT_OK)

    async def main():
        s.request_extraction("cs-empty", [])
        await _drain(s)

    asyncio.run(main())
    assert g.calls["extract"] == 0
    assert db.docs.get("memory_meta", {}) == {}


# ── Dedup ────────────────────────────────────────────────────────────────────

def test_dedup_merges_same_memory_and_appends_provenance(make_memory):
    factory, _, _ = make_memory
    s, db, g = factory(extract_result={"memories": [
        {"type": "episodic", "text": "User and assistant discussed peacock colors",
         "topics": ["birds"], "turn_refs": [1]},
    ]})

    async def run_twice():
        s.request_extraction("cs-1", _snapshot()[:1])
        await _drain(s)
        # Second conversation, same subject → must merge, not duplicate.
        s.request_extraction("cs-2", _snapshot()[:1])
        await _drain(s)

    asyncio.run(run_twice())

    mems = list(db.docs.get("memories", {}).values())
    assert len(mems) == 1
    m = mems[0]
    assert m["times_seen"] == 2
    assert [p["session_ref"] for p in m["provenance"]] == ["cs-1", "cs-2"]


def test_distinct_memories_not_deduped(make_memory):
    factory, _, _ = make_memory
    s, db, g = factory(extract_result={"memories": [
        {"type": "semantic", "text": "Alpha fact"},
        {"type": "semantic", "text": "Beta fact"},
    ]})
    g.embeddings_by_text = {"Alpha fact": [1.0, 0.0], "Beta fact": [0.0, 1.0]}

    async def main():
        s.request_extraction("cs-1", _snapshot())
        await _drain(s)

    asyncio.run(main())
    assert len(db.docs.get("memories", {})) == 2


# ── Retrieval ────────────────────────────────────────────────────────────────

def _seed_memories(db, entries):
    coll = db.docs.setdefault("memories", {})
    for i, e in enumerate(entries):
        coll[f"m{i}"] = {
            "id": f"m{i}",
            "deleted": False,
            "created_ms": i,
            **e,
        }


def test_recall_ranks_by_cosine(make_memory):
    factory, _, _ = make_memory
    s, db, g = factory()
    g.embeddings_by_text["did we talk about birds"] = [1.0, 0.0, 0.0]
    _seed_memories(db, [
        {"type": "episodic", "text": "discussed peacocks",
         "embedding": [0.9, 0.1, 0.0]},
        {"type": "semantic", "text": "likes trains",
         "embedding": [0.0, 0.0, 1.0]},
    ])

    out = asyncio.run(s.recall("did we talk about birds", top_k=2))
    assert out[0]["text"] == "discussed peacocks"
    assert out[0]["score"] > out[1]["score"]
    assert asyncio.run(s.recall("   ")) == []


def test_recall_block_is_bounded_and_structured(make_memory):
    factory, _, _ = make_memory
    s, db, g = factory()
    eps, facts = [], []
    for i in range(15):
        eps.append({
            "type": "episodic", "text": f"episode {i}",
            "embedding": [float(i % 3), 1.0, 0.0],
            "provenance": [{"started_at": "2026-08-22T10:00:00+00:00"}],
        })
    for i in range(10):
        facts.append({
            "type": "task" if i % 2 else "semantic", "text": f"fact {i}",
            "embedding": [1.0, float(i % 3), 0.0],
        })
    _seed_memories(db, eps + facts)

    block = asyncio.run(s.recall_block())
    assert block.startswith("\n\nLong-term memory")
    assert len(block) <= memory_mod.INJECT_MAX_CHARS
    # Episodic lines are date-stamped ("22 Aug 2026 — episode n").
    assert block.count("episode ") == memory_mod.EPISODES_IN_INJECT
    assert block.count("- fact ") == memory_mod.FACTS_IN_INJECT
    assert "22 Aug 2026" in block          # episodic date stamp


def test_recall_block_empty_when_no_memories(make_memory):
    factory, _, _ = make_memory
    s, db, _ = factory()
    assert asyncio.run(s.recall_block()) == ""


# ── User controls ────────────────────────────────────────────────────────────

def test_list_and_soft_delete(make_memory):
    factory, _, _ = make_memory
    s, db, _ = factory()
    _seed_memories(db, [
        {"type": "semantic", "text": "keep me", "embedding": [1.0],
         "times_seen": 3, "last_seen_at": "now"},
        {"type": "episodic", "text": "delete me", "embedding": [0.5]},
    ])

    listed = asyncio.run(s.list_memories())
    assert len(listed) == 2

    assert asyncio.run(s.soft_delete("m1")) is True
    listed = asyncio.run(s.list_memories())
    assert [m["text"] for m in listed] == ["keep me"]
    assert db.docs["memories"]["m1"]["deleted"] is True       # soft, not gone
    assert asyncio.run(s.soft_delete("missing")) is False


def test_deleted_excluded_from_recall_and_inject(make_memory):
    factory, _, _ = make_memory
    s, db, g = factory()
    g.embeddings_by_text["query"] = [1.0]
    _seed_memories(db, [
        {"type": "semantic", "text": "gone", "embedding": [1.0], "deleted": True},
    ])
    assert asyncio.run(s.recall("query")) == []
    assert asyncio.run(s.recall_block()) == ""
    assert asyncio.run(s.list_memories()) == []


# ── Null store ───────────────────────────────────────────────────────────────

def test_null_memory_store():
    n = NullMemoryStore()
    assert n.enabled() is False
    n.request_extraction("x")
    assert asyncio.run(n.recall("q")) == []
    assert asyncio.run(n.recall_block()) == ""
    assert asyncio.run(n.list_memories()) == []
    assert asyncio.run(n.soft_delete("x")) is False


# ── Session deletion + memory cascade ┚─ P3 add-on ─────────────────────────

def test_strip_provenance_updates_and_orphans_die(make_memory):
    """Memory cited by 2 sessions survives a delete with provenance stripped;
    memory cited only by the deleted session is removed outright."""
    factory, _, _ = make_memory
    s, db, _ = factory()

    async def seed():
        coll = db.docs.setdefault("memories", {})
        coll["m-shared"] = {
            "deleted": False, "created_ms": 1,
            "text": "discussed peacocks",
            "provenance": [
                {"session_ref": "s-A", "turn_ids": ["t1"]},
                {"session_ref": "s-B", "turn_ids": ["t9"]},
            ],
        }
        coll["m-orphan"] = {
            "deleted": False, "created_ms": 2,
            "text": "only in A",
            "provenance": [{"session_ref": "s-A", "turn_ids": ["t2"]}],
        }
        coll["m-unrelated"] = {
            "deleted": False, "created_ms": 3,
            "text": "other topic",
            "provenance": [{"session_ref": "s-C", "turn_ids": ["t3"]}],
        }

    async def main():
        await seed()
        stats = await s.strip_provenance("s-A")
        await s.delete_session_meta("s-A")

    asyncio.run(main())

    mems = db.docs["memories"]
    assert [p["session_ref"] for p in mems["m-shared"]["provenance"]] == ["s-B"]
    assert "m-orphan" not in mems          # sourceless → hard-deleted
    assert "m-unrelated" in mems           # untouched


def test_null_stores_cascade_noops():
    n = NullMemoryStore()
    assert asyncio.run(n.strip_provenance("x")) == {
        "memories_updated": 0, "memories_deleted": 0
    }


# ── REST API ─────────────────────────────────────────────────────────────────

class StubStore:
    def __init__(self):
        self.deleted = []

    async def recall(self, q, top_k=8):
        return [{"id": "m0", "type": "episodic", "text": f"hit:{q}",
                 "score": 0.99, "topics": [], "entities": []}][:top_k]

    async def recall_block(self):
        return "\n\nLong-term memory:\n- x"

    async def list_memories(self, limit=100):
        return [{"id": "m0", "type": "semantic", "text": "stub"}][:limit]

    def enabled(self):
        return True


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main_mod, "AUTH_TOKEN", "secret")
    reset_memory_store_for_tests()
    return TestClient(main_mod.app)


def test_rest_memories_require_token(client):
    assert client.get("/memories").status_code == 401
    assert client.delete("/memories/m0").status_code == 401


def test_rest_memories_list_and_query(client, monkeypatch):
    stub = StubStore()
    monkeypatch.setattr(main_mod, "get_memory_store", lambda: stub)
    r = client.get("/memories", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    assert r.json()["memories"][0]["text"] == "stub"

    r = client.get(
        "/memories?q=birds&limit=5",
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "birds"
    assert body["memories"][0]["text"] == "hit:birds"


def test_rest_delete_memory(client, monkeypatch):
    stub = StubStore()
    deleted_log: list[str] = []

    async def sd(mid):
        deleted_log.append(mid)
        return mid != "unknown"

    stub.soft_delete = sd
    monkeypatch.setattr(main_mod, "get_memory_store", lambda: stub)

    ok = client.delete("/memories/m0", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200 and ok.json() == {"deleted": True, "id": "m0"}
    assert deleted_log == ["m0"]

    missing = client.delete(
        "/memories/unknown", headers={"Authorization": "Bearer secret"}
    )
    assert missing.status_code == 404


def test_rest_503_when_memory_store_errors(client, monkeypatch):
    class Boom:
        async def list_memories(self, limit=100):
            raise RuntimeError("firestore down")

        async def recall(self, q, top_k=8):
            raise RuntimeError("firestore down")

        async def soft_delete(self, mid):
            raise RuntimeError("firestore down")

    monkeypatch.setattr(main_mod, "get_memory_store", lambda: Boom())
    h = {"Authorization": "Bearer secret"}
    assert client.get("/memories", headers=h).status_code == 503
    assert client.delete("/memories/m0", headers=h).status_code == 503


# ── Selection / wiring sanity ───────────────────────────────────────────────

def test_get_memory_store_env_selection(monkeypatch):
    reset_memory_store_for_tests()
    monkeypatch.delenv("SIRIOUS_MEMORY", raising=False)
    assert get_memory_store().enabled() is False
    reset_memory_store_for_tests()
    monkeypatch.setenv("SIRIOUS_MEMORY", "1")
    assert isinstance(get_memory_store(), MemoryStore)
    reset_memory_store_for_tests()


def test_main_imports_memory_hook():
    # The WS handler must reference the memory store hook (wired in P3).
    import inspect
    src = inspect.getsource(main_mod.websocket_endpoint)
    assert "request_extraction" in src
    assert "recall_block" in src
