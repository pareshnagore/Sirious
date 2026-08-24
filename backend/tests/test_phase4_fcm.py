"""Phase 4 chunk 3 tests: FCM device registry + push delivery.

Run from backend/:  .venv/Scripts/python.exe -m pytest tests/ -q
No Firebase project, network, or credentials needed — HTTP and auth are
faked; Firestore via the same FakeDb pattern as test_phase4.
"""

import asyncio

from app import fcm as fcm_mod
from app.fcm import DeviceTokenStore, send_push


# ── FakeDb (same shape as test_phase4's) ─────────────────────────────────────

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

    async def get(self, transaction=None):
        return _Snap(self.id, self.db.get(self.coll, {}).get(self.id))

    async def delete(self):
        self.db.get(self.coll, {}).pop(self.id, None)


class _Coll:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def document(self, doc_id):
        return _DocRef(self.db, self.name, doc_id)

    def limit(self, n):
        return self

    def stream(self):
        docs = self.db.get(self.name, {})

        class _Iter:
            def __aiter__(self):
                self._items = list(docs.items())
                self._i = 0
                return self

            async def __anext__(self):
                if self._i >= len(self._items):
                    raise StopAsyncIteration
                k, v = self._items[self._i]
                self._i += 1
                snap = _Snap(k, v)
                return snap

        return _Iter()


class FakeDb:
    def __init__(self):
        self.db = {}

    def collection(self, name):
        return _Coll(self.db, name)


# ── DeviceTokenStore ─────────────────────────────────────────────────────────

def test_register_creates_hashed_doc():
    fake_db = FakeDb()
    store = DeviceTokenStore()
    store._ensure_db = lambda: fake_db
    doc_id = asyncio.run(store.register("tok-abc", platform="android"))
    assert len(doc_id) == 64  # sha256 hex
    doc = fake_db.db[fcm_mod.DEVICE_TOKENS_COLLECTION][doc_id]
    assert doc["token"] == "tok-abc"
    assert doc["platform"] == "android"
    assert doc["registered_at"]


def test_re_register_overwrites_not_duplicates():
    fake_db = FakeDb()
    store = DeviceTokenStore()
    store._ensure_db = lambda: fake_db
    id1 = asyncio.run(store.register("tok-abc"))
    id2 = asyncio.run(store.register("tok-abc"))
    assert id1 == id2
    assert len(fake_db.db[fcm_mod.DEVICE_TOKENS_COLLECTION]) == 1


def test_remove_deletes_doc():
    fake_db = FakeDb()
    store = DeviceTokenStore()
    store._ensure_db = lambda: fake_db
    doc_id = asyncio.run(store.register("tok-abc"))
    asyncio.run(store.remove("tok-abc"))
    assert doc_id not in fake_db.db[fcm_mod.DEVICE_TOKENS_COLLECTION]


def test_list_all_returns_registrations():
    fake_db = FakeDb()
    store = DeviceTokenStore()
    store._ensure_db = lambda: fake_db
    asyncio.run(store.register("tok-a"))
    asyncio.run(store.register("tok-b"))
    devices = asyncio.run(store.list_all())
    assert {d["token"] for d in devices} == {"tok-a", "tok-b"}


# ── send_push ────────────────────────────────────────────────────────────────

class _FakeUrlopen:
    """Context-manager stand-in for urllib.request.urlopen."""
    next_result = None  # (status, body) or Exception instance

    def __init__(self, result):
        object.__setattr__(self, "_result", result)

    def __enter__(self):
        r = type(self).next_result
        if isinstance(r, Exception):
            raise r
        status, body = r
        self.status = status
        self.read = lambda: body.encode()
        return self

    def __exit__(self, *exc):
        return False


def test_send_push_success(monkeypatch):
    _FakeUrlopen.next_result = (200, '{"name":"projects/p/m/x"}')
    monkeypatch.setattr(
        fcm_mod.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopen(None),
    )
    monkeypatch.setattr(
        fcm_mod, "_get_access_token_cached", lambda cache: "fake-token"
    )
    ok, status = send_push("dev-token", "Reminder", "call Raj")
    assert ok is True and status == "ok"


def test_send_push_unregistered(monkeypatch):
    _FakeUrlopen.next_result = fcm_mod.urllib.error.HTTPError(
        "url", 404, "Not Found", {}, None
    )
    monkeypatch.setattr(
        fcm_mod.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopen(None),
    )
    monkeypatch.setattr(
        fcm_mod, "_get_access_token_cached", lambda cache: "fake-token"
    )
    ok, status = send_push("dead-token", "t", "b")
    assert ok is False and status == "unregistered"


def test_send_push_transient_failure(monkeypatch):
    _FakeUrlopen.next_result = fcm_mod.urllib.error.HTTPError(
        "url", 500, "boom", {}, None
    )
    monkeypatch.setattr(
        fcm_mod.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeUrlopen(None),
    )
    monkeypatch.setattr(
        fcm_mod, "_get_access_token_cached", lambda cache: "fake-token"
    )
    ok, status = send_push("dev-token", "t", "b")
    assert ok is False and status.startswith("http_")


def test_deliver_reminder_prunes_unregistered_and_counts(monkeypatch):
    fake_db = FakeDb()
    store = DeviceTokenStore()
    store._ensure_db = lambda: fake_db
    asyncio.run(store.register("tok-good"))
    asyncio.run(store.register("tok-dead"))

    calls = []

    def fake_send(token, title, body, data=None, token_cache=None):
        calls.append(token)
        if token == "tok-dead":
            return False, "unregistered"
        return True, "ok"

    monkeypatch.setattr(fcm_mod, "send_push", fake_send)
    stats = asyncio.run(
        fcm_mod.deliver_reminder_to_all_devices(
            "call Raj", "rem-1", store
        )
    )
    assert stats["devices"] == 2
    assert stats["sent"] == 1
    assert stats["pruned"] == 1
    # dead token removed from the registry
    all_tokens = [d["token"] for d in asyncio.run(store.list_all())]
    assert all_tokens == ["tok-good"]
