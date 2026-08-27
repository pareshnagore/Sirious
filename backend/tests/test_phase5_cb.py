"""Phase 5 C+B tests: unified ambient+voice session docs.

Covers:
  - _replay_block: typed ambient ("S1: …") + voice (User/You) system block
  - store.replay_turns: typed entries, order + empty-drop
  - store._apply_start: re-seeds the in-memory buffer when a voice session
    continues an existing ambient doc (protects ambient turns from clobber)
  - store.snapshot_turns: excludes ambient-only turns from memory extraction
  - store.list_sessions: preview falls back to the last room utterance
  - GET /sessions/{id}: per-turn kind shapes for mixed docs

No network: Firestore is faked exactly like test_phase2.py. Store methods are
sync but spawn an asyncio writer task, so every scenario runs inside one loop
(asyncio.run) like the phase2 tests.
"""

import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)  # backend root → app.main / app.store

from app import store as store_mod  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.main import _replay_block  # noqa: E402


# ── Fake Firestore (mirrors test_phase2.py) ──────────────────────────────

class _Snap:
    def __init__(self, id, data):
        self.id = id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class FakeDocRef:
    def __init__(self, db, doc_id):
        self.db = db
        self.id = doc_id

    async def get(self):
        return _Snap(self.id, self.db.docs.get(self.id))

    async def set(self, data, merge=False):
        cur = self.db.docs.setdefault(self.id, {})
        if merge:
            cur.update(data)
        else:
            cur.clear()
            cur.update(data)


class FakeCollection:
    def __init__(self, db):
        self.db = db
        self._limit = 50

    def document(self, doc_id):
        return FakeDocRef(self.db, doc_id)

    def order_by(self, *a, **k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    async def stream(self):
        items = sorted(
            self.db.docs.items(),
            key=lambda kv: kv[1].get("updated_ms", 0),
            reverse=True,
        )[: self._limit]
        for doc_id, data in items:
            yield _Snap(doc_id, data)


class FakeDB:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return FakeCollection(self)


@pytest.fixture
def make_store(monkeypatch):
    db = FakeDB()

    def factory():
        s = store_mod.SessionStore()
        monkeypatch.setattr(s, "_ensure_db", lambda: db)
        return s

    return factory, db


async def _drain(store):
    if store._queue is not None:
        await store._queue.join()


def _ambient_turn(store, doc_id, speaker, text, now_iso="2026-08-26T00:00:00+00:00"):
    store.record_ambient_turn(
        doc_id, speaker_tag=speaker, text=text, start_s=0.0, end_s=3.0, now_iso=now_iso
    )


def _voice_turn(store, doc_id, user, assistant, now_iso="2026-08-26T00:00:10+00:00"):
    store.record_turn(
        doc_id,
        summary={
            "turn_id": f"t-{now_iso}",
            "started_at": now_iso,
            "ended_at": now_iso,
            "user_text": user,
            "assistant_text": assistant,
            "interrupted": False,
            "reason": "turn_complete",
        },
        now_iso=now_iso,
    )


# ── _replay_block ─────────────────────────────────────────────────────────

def test_replay_block_empty_inputs():
    assert _replay_block([]) == ""
    assert _replay_block(None) == ""
    assert _replay_block([{"kind": "ambient", "speaker": 1, "text": "   "}]) == ""


def test_replay_block_mixed_ambient_and_voice():
    block = _replay_block(
        [
            {"kind": "ambient", "speaker": 1, "text": "did you deploy it?"},
            {
                "kind": "voice",
                "user_text": "Sirious, can you answer that?",
                "assistant_text": "Yes, the deployment is done.",
            },
            {"kind": "ambient", "speaker": 2, "text": ""},  # dropped
        ]
    )
    assert "S1: did you deploy it?" in block
    assert "User said: Sirious, can you answer that?" in block
    assert "You replied: Yes, the deployment is done." in block
    assert "S2:" not in block
    assert block.startswith("\n\nThe following is context around this conversation")


def test_replay_block_voice_only_backwards_compatible():
    block = _replay_block(
        [
            {"kind": "voice", "user_text": "hi", "assistant_text": "hello"},
        ]
    )
    assert "User said: hi" in block
    assert "You replied: hello" in block


# ── replay_turns (typed) ───────────────────────────────────────────────────

def test_replay_turns_typed_entries(make_store):
    factory, _ = make_store

    async def scenario():
        s = factory()
        s.start_session("amb-doc", client_session_id="amb-doc", model="x",
                        resumed=False, device=None, now_iso="2026-08-26T00:00:00+00:00",
                        mode="ambient")
        _ambient_turn(s, "amb-doc", 1, "did you deploy it?")
        _voice_turn(s, "amb-doc", "Sirious, answer?", "Deployed.")
        _ambient_turn(s, "amb-doc", 2, "great!")
        s.record_ambient_turn("amb-doc", speaker_tag=3, text="   ", start_s=0,
                              end_s=1, now_iso="2026-08-26T00:00:30+00:00")
        await _drain(s)
        return await s.replay_turns("amb-doc")

    entries = asyncio.run(scenario())
    kinds = [e["kind"] for e in entries]
    assert kinds == ["ambient", "voice", "ambient"]
    assert entries[0]["text"] == "did you deploy it?"
    assert entries[1]["user_text"] == "Sirious, answer?"
    assert entries[2]["speaker"] == 2
    # whitespace-only ambient turn dropped
    assert all(e.get("text", "").strip() for e in entries if e["kind"] == "ambient")


# ── _apply_start re-seeds buffer (unified doc protection) ──────────────────

def test_voice_start_on_ambient_doc_preserves_turns(make_store):
    factory, db = make_store

    async def scenario():
        # 1) ambient session: room turns land in Firestore
        s = factory()
        s.start_session("amb-doc", client_session_id="amb-doc", model="deepgram-nova-3",
                        resumed=False, device=None, now_iso="2026-08-26T00:00:00+00:00",
                        mode="ambient")
        _ambient_turn(s, "amb-doc", 1, "did you deploy it?")
        _ambient_turn(s, "amb-doc", 2, "yes, it is live")
        s.end_session("amb-doc", now_iso="2026-08-26T00:00:05+00:00",
                      reason="client_stop", audio_in_bytes=0, audio_out_bytes=0)
        await _drain(s)
        assert len(db.docs["amb-doc"]["turns"]) == 2
        assert db.docs["amb-doc"].get("title") == "did you deploy it?"

        # 2) voice leg reuses the SAME client_session_id (Phase 5 C+B)
        s2 = factory()
        s2.start_session("amb-doc", client_session_id="amb-doc", model="gemini-x",
                         resumed=False, device=None, now_iso="2026-08-26T00:00:10+00:00")
        _voice_turn(s2, "amb-doc", "Sirious, answer?", "Deployed.")
        s2.end_session("amb-doc", now_iso="2026-08-26T00:00:20+00:00",
                       reason="turn_complete", audio_in_bytes=1, audio_out_bytes=1)
        await _drain(s2)
        return db.docs["amb-doc"]

    doc = asyncio.run(scenario())
    # 3) ALL turns survive — ambient NOT clobbered; title stays the room turn
    turns = doc["turns"]
    kinds = [t.get("kind") for t in turns]
    assert kinds == ["ambient", "ambient", None]  # voice turns have no kind key
    texts = [t.get("text") for t in turns[:2]]
    assert texts == ["did you deploy it?", "yes, it is live"]
    assert doc["title"] == "did you deploy it?"
    assert doc["resume_count"] == 1


# ── snapshot_turns excludes ambient ────────────────────────────────────────

def test_snapshot_turns_excludes_ambient(make_store):
    factory, _ = make_store

    async def scenario():
        s = factory()
        s.start_session("amb-doc", client_session_id="amb-doc", model="x",
                        resumed=False, device=None, now_iso="2026-08-26T00:00:00+00:00",
                        mode="ambient")
        _ambient_turn(s, "amb-doc", 1, "did you deploy it?")
        _voice_turn(s, "amb-doc", "Sirious, answer?", "Deployed.")
        await _drain(s)  # buffer turns materialize in the writer task
        return s.snapshot_turns("amb-doc")

    snap = asyncio.run(scenario())
    assert len(snap) == 1
    assert snap[0]["user_text"] == "Sirious, answer?"
    assert snap[0]["assistant_text"] == "Deployed."


# ── list_sessions preview fallback ─────────────────────────────────────────

def test_list_sessions_preview_falls_back_to_room_text(make_store):
    factory, _ = make_store

    async def scenario():
        s = factory()
        s.start_session("amb-a", client_session_id="amb-a", model="x", resumed=False,
                        device=None, now_iso="2026-08-26T00:00:00+00:00", mode="ambient")
        s.record_ambient_turn("amb-a", speaker_tag=1,
                              text="hey sirious what is the weather",
                              start_s=0, end_s=2, now_iso="2026-08-26T00:00:01+00:00")
        s.start_session("voice-b", client_session_id="voice-b", model="x", resumed=False,
                        device=None, now_iso="2026-08-26T00:00:02+00:00")
        _voice_turn(s, "voice-b", "hello", "hi there! this is the answer text")
        await _drain(s)
        return await s.list_sessions()

    items = asyncio.run(scenario())
    by_id = {i["id"]: i for i in items}
    assert by_id["amb-a"]["preview"] == "hey sirious what is the weather"
    assert by_id["voice-b"]["preview"] == "hi there! this is the answer text"
    assert by_id["amb-a"]["mode"] == "ambient"


# ── GET /sessions/{id}: mixed turn shapes ──────────────────────────────────

def test_get_session_mixed_turn_shapes(make_store, monkeypatch):
    factory, _ = make_store

    async def scenario():
        s = factory()
        s.start_session("amb-doc", client_session_id="amb-doc", model="x",
                        resumed=False, device=None, now_iso="2026-08-26T00:00:00+00:00",
                        mode="ambient")
        _ambient_turn(s, "amb-doc", 1, "did you deploy it?")
        _voice_turn(s, "amb-doc", "Sirious, answer?", "Deployed.")
        await _drain(s)
        monkeypatch.setattr(main_mod, "get_store", lambda: s)
        return await main_mod.get_session("amb-doc", None)

    resp = asyncio.run(scenario())
    assert resp["mode"] == "ambient"
    assert resp["title"] == "did you deploy it?"
    turns = resp["turns"]
    assert turns[0]["kind"] == "ambient"
    assert turns[0]["speaker"] == 1
    assert turns[0]["text"] == "did you deploy it?"
    assert turns[1]["kind"] == "voice"
    assert turns[1]["user_text"] == "Sirious, answer?"
    assert turns[1]["assistant_text"] == "Deployed."