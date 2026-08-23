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
import base64
import collections
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("sirious.tools")

NOTES_COLLECTION = "tool_notes"
AUDIT_COLLECTION = "tool_audit"
REMINDERS_COLLECTION = "reminders"
MAX_NOTE_CHARS = 4000
MAX_SNIPPET_CHARS = 350
MAX_AUDIT_ARG_CHARS = 500
# Reminders: a draft is only a proposal until confirm_reminder schedules it.
# Drafts expire so abandoned drafts never pile up as schedulable state.
DRAFT_TTL_S = 15 * 60               # unconfirmed draft → stale after 15 min
MAX_REMINDER_TEXT_CHARS = 500
MIN_LEAD_S = 120                    # must fire ≥ 2 min from now (voice UX)
MAX_HORIZON_DAYS = 365              # sanity cap on how far out a reminder may be
FIRE_GRACE_S = 300                  # fire accepted within 5 min BEFORE due (clock skew)


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


# ── Reminders ────────────────────────────────────────────────────────────────

def _validate_window(ts: float, *, now_s: float) -> tuple[float | None, str | None]:
    """Shared sanity window: ≥ MIN_LEAD_S ahead, ≤ MAX_HORIZON_DAYS out."""
    if ts < now_s + MIN_LEAD_S:
        return None, (
            "due_at must be at least "
            f"{MIN_LEAD_S // 60} minutes in the future"
        )
    if ts > now_s + MAX_HORIZON_DAYS * 86400:
        return None, f"due_at is more than {MAX_HORIZON_DAYS} days away"
    return ts, None


def _parse_due_at(raw: Any, *, now_s: float) -> tuple[float | None, str | None]:
    """Parse an ISO-8601 ``due_at`` into a UTC unix timestamp.

    Accepts explicit offsets ("2026-08-28T09:00:00+05:30"), Z suffix, and
    naive stamps (interpreted in the USER'S timezone — see _user_tz — since
    an offset-less time most plausibly means the user's local wall clock).
    Returns (ts, error); unparsable input yields "could not parse …" so the
    caller can fall back to natural-language resolution.
    """
    if not raw:
        return None, "empty due_at"
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None, (
            f"could not parse due_at '{raw}' — use ISO 8601 like "
            f"2026-08-28T09:00:00+05:30"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_user_tz())
    return _validate_window(dt.timestamp(), now_s=now_s)


# ── Natural-language "when" resolution ───────────────────────────────────────
#
# The model passes the USER'S OWN PHRASING ("friday morning", "in 20
# minutes"); the server — not the model — knows the current time and
# timezone. This keeps temporal knowledge out of system_instruction
# (stable prompt prefix across reconnects, no stale-clock sessions).

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_PERIOD_TIMES = {          # bare period word → default clock time
    "morning": (9, 0), "afternoon": (15, 0),
    "evening": (19, 0), "night": (21, 0),
}
_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12,
}
_WHEN_EXAMPLES = (
    "'in 20 minutes', 'tomorrow 9 am', 'friday morning', "
    "'next monday 2 pm', 'tonight', 'day after tomorrow at 5 pm'"
)

_DAY_RE = re.compile(
    r"(?P<next>next\s+)?\b(today|tonight|tomorrow|day\s+after\s+tomorrow|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)
_CLOCK_RE = re.compile(
    r"(?:\bat\s+)?\b(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*"
    r"(?P<ap>a\.?m\.?|p\.?m\.?)?(?![\d:])"
)
_PERIOD_RE = re.compile(r"\b(morning|afternoon|evening|night)\b")
_RELATIVE_RE = re.compile(
    r"in\s+(?P<n>a|an|\d+|[a-z]+)\s*"
    r"(?P<unit>minutes?|mins?|hours?|hrs?|days?|weeks?)\b"
)


def _user_tz():
    """The user's timezone (SIRIOUS_TZ, default Asia/Kolkata). Every user-
    facing time phrase resolves against this, never the server's clock zone."""
    return ZoneInfo(os.environ.get("SIRIOUS_TZ", "Asia/Kolkata"))


def _resolve_when(text: Any, *, now_s: float) -> tuple[Any | None, str | None]:
    """Resolve plain-English ``when`` against the user's timezone.

    Closed grammar (promised verbatim in the tool description):
      in N minutes|hours|days|weeks · today/tonight/tomorrow/
      day after tomorrow · [next] <weekday> · H[:MM] [am|pm] ·
      noon implied by period words morning/afternoon/evening/night.
    Returns (aware UTC datetime | None, error | None). Errors enumerate the
    supported forms so the model can re-ask the user conversationally.
    """
    s = " ".join(str(text or "").strip().lower().split())
    if not s:
        return None, "empty due_at"
    now = datetime.fromtimestamp(now_s, tz=_user_tz())

    # 1) Pure relative duration: "in 20 minutes", "in five hours".
    m_rel = _RELATIVE_RE.search(s)
    if m_rel and m_rel.group(0) and s.split()[0] in ("in",):
        n = _WORD_NUMBERS.get(m_rel.group("n"))
        if n is None:
            try:
                n = int(m_rel.group("n"))
            except ValueError:
                return None, (
                    f"couldn't understand '{m_rel.group('n')}' in '{s}' — "
                    f"use {_WHEN_EXAMPLES}"
                )
        unit = m_rel.group("unit")
        if unit.startswith(("min",)):
            delta = timedelta(minutes=n)
        elif unit.startswith(("h",)):
            delta = timedelta(hours=n)
        elif unit.startswith("d"):
            delta = timedelta(days=n)
        else:
            delta = timedelta(weeks=n)
        return now + delta, None

    # 2) Day reference.
    day_offset = 0
    had_day_token = False
    weekday_plain = False
    m_day = _DAY_RE.search(s)
    if m_day:
        had_day_token = True
        word = m_day.group(2)
        if word == "today":
            day_offset = 0
        elif word == "tonight":
            day_offset = 0
        elif word == "tomorrow":
            day_offset = 1
        elif word.startswith("day"):
            day_offset = 2
        else:
            ahead = (_WEEKDAYS[word] - now.weekday()) % 7
            if m_day.group("next"):
                ahead += 7                      # "next friday" ≠ this friday
            else:
                weekday_plain = True
            day_offset = ahead

    # 3) Explicit clock time (H[:MM] [am|pm]) — first plausible match.
    hour = minute = None
    m_clock = _CLOCK_RE.search(s)
    if m_clock:
        h = int(m_clock.group("h"))
        mi = int(m_clock.group("mi") or 0)
        ap = m_clock.group("ap")
        if ap:
            ap = ap.replace(".", "")
            if ap == "am":
                h = 0 if h == 12 else h
            else:
                h = 12 if h == 12 else h + 12
        elif h <= 12:
            m_period_here = _PERIOD_RE.search(s)
            if m_period_here and m_period_here.group(1) != "morning" and h <= 11:
                h += 12                          # "5 evening" → 17:00
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None, (
                f"clock time '{m_clock.group(0).strip()}' in '{s}' is out of "
                f"range — use {_WHEN_EXAMPLES}"
            )
        hour, minute = h, mi

    # 4) Defaults when no explicit clock time.
    m_period = _PERIOD_RE.search(s)
    if hour is None:
        if m_period:
            hour, minute = _PERIOD_TIMES[m_period.group(1)]
        elif s.startswith("tonight"):
            hour, minute = (21, 0)
        elif had_day_token:
            hour, minute = (9, 0)               # bare day → 09:00 (v1 default)
        else:
            return None, (
                f"could not understand when '{s}' is — use ISO 8601 "
                f"(e.g. 2026-08-28T09:00:00+05:30) or simple English like "
                f"{_WHEN_EXAMPLES}"
            )

    base = (now + timedelta(days=day_offset)).date()
    dt = datetime(
        base.year, base.month, base.day, hour, minute, tzinfo=now.tzinfo,
    )

    # Forgiving bumps BEFORE the strict window check: a bare weekday or
    # clock time that already passed rolls forward instead of erroring.
    if dt <= now:
        if not had_day_token:
            dt += timedelta(days=1)             # "at 5pm" said at 6pm → tomorrow
        elif weekday_plain:
            dt += timedelta(days=7)             # "friday" said friday night

    return dt.astimezone(timezone.utc), None


def _resolve_due(raw: Any, *, now_s: float) -> tuple[float | None, str | None]:
    """Single entry point used by the create handler: ISO first, then the
    natural-language grammar. Out-of-range ISO errors surface as-is (an
    explicit wrong date shouldn't be silently reinterpreted)."""
    ts, err = _parse_due_at(raw, now_s=now_s)
    if ts is not None:
        return ts, None
    if err and "could not parse" not in err and err != "empty due_at":
        return None, err
    dt, nl_err = _resolve_when(raw, now_s=now_s)
    if nl_err is not None:
        return None, nl_err
    return _validate_window(dt.timestamp(), now_s=now_s)


class ReminderStore:
    """Reminders in Firestore.

    Lifecycle: create_reminder writes status="draft"; confirm_reminder flips
    it to "scheduled" (chunk 2 attaches Cloud Tasks there); cancel_reminder
    sets "cancelled". The fire endpoint (chunk 2) transitions scheduled → fired.
    Direct awaits are fine here — same reasoning as NotesStore: tool execution
    sits outside the audio hot path.
    """

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

    async def create_draft(
        self,
        *,
        text: str,
        due_ts: float,
        due_iso: str,
        doc_id: str,
        created_at: str,
    ) -> str:
        db = self._ensure_db()
        reminder_id = uuid.uuid4().hex
        await (
            db.collection(REMINDERS_COLLECTION)
            .document(reminder_id)
            .set(
                {
                    "text": text,
                    "due_at": due_iso,
                    "due_ts": due_ts,
                    "topic": None,
                    "status": "draft",
                    "session_ref": doc_id,
                    "created_at": created_at,
                }
            )
        )
        return reminder_id

    async def get_status(self, reminder_id: str) -> dict[str, Any] | None:
        snap = await (
            self._ensure_db()
            .collection(REMINDERS_COLLECTION)
            .document(reminder_id)
            .get()
        )
        return snap.to_dict() if snap.exists else None

    async def set_status(self, reminder_id: str, status: str) -> None:
        await (
            self._ensure_db()
            .collection(REMINDERS_COLLECTION)
            .document(reminder_id)
            .set(
                {"status": status, "updated_at": _utcnow_iso()},
                merge=True,
            )
        )

    async def set_task(self, reminder_id: str, task_name: str | None) -> None:
        await (
            self._ensure_db()
            .collection(REMINDERS_COLLECTION)
            .document(reminder_id)
            .set(
                {"task_name": task_name, "updated_at": _utcnow_iso()},
                merge=True,
            )
        )

    async def get_status_for_update(
        self, reminder_id: str
    ) -> dict[str, Any] | None:
        return await self.get_status(reminder_id)

    async def mark_fired(self, reminder_id: str) -> None:
        """Status flip with an idempotency precondition: the write only lands
        while status == "scheduled", enforced inside a Firestore transaction.
        A concurrent second fire sees status="fired" inside its own
        transaction and raises _AlreadyFired — process_fired_reminder maps
        that to a 200 duplicate-suppressed response."""
        db = self._ensure_db()
        ref = db.collection(REMINDERS_COLLECTION).document(reminder_id)

        async def _tx(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise KeyError("reminder vanished during fire")
            if (snap.to_dict() or {}).get("status") != "scheduled":
                raise _AlreadyFired()
            transaction.set(
                ref,
                {
                    "status": "fired",
                    "fired_at": _utcnow_iso(),
                    "updated_at": _utcnow_iso(),
                },
                merge=True,
            )

        try:
            await db.transaction(_tx)
        except (_AlreadyFired, KeyError):
            raise

    async def get_task_name(self, reminder_id: str) -> str | None:
        data = await self.get_status(reminder_id)
        return (data or {}).get("task_name")


# ── Reminder scheduling backends (chunk 2) ───────────────────────────────────
#
# confirm_reminder schedules ONE one-shot Cloud Tasks HTTP task at the due
# instant; when it fires it hits our /internal/fire-reminder endpoint with an
# OIDC Cloud Tasks service-agent token. No polling anywhere.

class NullScheduler:
    """Chunk-1 behavior: record consent, schedule nothing. Used when
    SIRIOUS_TASKS_QUEUE is unset (local dev, tests, staged rollouts)."""

    async def schedule(self, reminder_id: str, due_ts: float) -> str | None:
        return None

    async def cancel(self, task_name: str) -> None:
        return None


class CloudTasksScheduler:
    """One-shot HTTPS task per confirmed reminder via google-cloud-tasks.

    - ``oidc_service_account``: the SA whose ID token Cloud Tasks attaches;
      must hold roles/iam.serviceAccountTokenCreator on ITSELF.
    - ``audience``/``target_url``: our own fire endpoint. Audience must equal
      the URL Cloud Run receives (custom-domain nuance).
    - Task names are DETERMINISTIC (reminder id + due second): a Cloud Tasks
      retry after an ambiguous timeout reuses the same name → no duplicate
      live tasks for one reminder.
    """

    def __init__(
        self,
        *,
        queue_path: str,
        target_url: str,
        oidc_service_account: str,
    ) -> None:
        self._queue_path = queue_path
        self._target_url = target_url
        self._oidc_sa = oidc_service_account
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from google.cloud import tasks_v2

            self._client = tasks_v2.CloudTasksAsyncClient()
        return self._client

    def _task_name(self, reminder_id: str, due_ts: float) -> str:
        suffix = base64.urlsafe_b64encode(
            str(int(due_ts)).encode()
        ).decode().rstrip("=")
        return f"{self._queue_path}/tasks/rem-{reminder_id}-{suffix}"

    async def schedule(self, reminder_id: str, due_ts: float) -> str | None:
        from google.cloud import tasks_v2
        from google.protobuf import timestamp_pb2

        client = self._ensure_client()
        proto = timestamp_pb2.Timestamp()
        proto.FromDatetime(datetime.fromtimestamp(due_ts, tz=timezone.utc))
        task = tasks_v2.Task(
            name=self._task_name(reminder_id, due_ts),
            http_request=tasks_v2.HttpRequest(
                url=self._target_url,
                http_method=tasks_v2.HttpMethod.POST,
                headers={
                    "Content-Type": "application/json",
                },
                body=json.dumps(
                    {"reminder_id": reminder_id}
                ).encode("utf-8"),
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=self._oidc_sa,
                    audience=self._target_url,
                ),
            ),
            schedule_time=proto,
        )
        created = await client.create_task(parent=self._queue_path, task=task)
        return created.name

    async def cancel(self, task_name: str) -> None:
        client = self._ensure_client()
        try:
            await client.delete_task(name=task_name)
        except Exception:  # noqa: BLE001 — already-run/gone is fine on cancel
            log.info("task delete failed (already gone?): %s", task_name)


class _AlreadyFired(Exception):
    """Internal: mark_fired precondition failed (already fired/cancelled)."""


def _scheduler_from_env() -> Any:
    """Build the scheduler from SIRIOUS_TASKS_* env vars; NullScheduler when
    unconfigured so local dev and tests never touch GCP."""
    queue = os.environ.get("SIRIOUS_TASKS_QUEUE")       # projects/p/…/queues/x
    target = os.environ.get("SIRIOUS_FIRE_URL")          # full fire-endpoint URL
    sa = os.environ.get("SIRIOUS_FIRE_OIDC_SA")           # signer SA email
    if not (queue and target and sa):
        return NullScheduler()
    return CloudTasksScheduler(
        queue_path=queue,
        target_url=target,
        oidc_service_account=sa,
    )


def verify_tasks_oidc(token: str, audience: str) -> tuple[str | None, str]:
    """Verify a Cloud Tasks OIDC token. Returns (email, error)."""
    try:
        from google.auth import jwt as google_jwt
    except ImportError:
        return None, "google-auth not installed"
    try:
        claims = google_jwt.decode(token, certs_url=_GOOGLE_CERTS_URL,
                                   audience=audience)
    except Exception as e:  # noqa: BLE001 — malformed/expired/wrong audience
        log.warning("fire-request OIDC rejected: %r", e)
        return None, "invalid or expired token"
    if claims.get("iss") != "https://cloud.google.com/iap":
        return None, "unexpected issuer"
    if claims.get("email_verified") is not True:
        return None, "email not verified"
    return claims.get("email"), ""


_GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"


class InMemoryReminderStore(ReminderStore):
    """Null-persistence stand-in (and test double)."""

    def __init__(self) -> None:
        super().__init__()
        self.reminders: dict[str, dict[str, Any]] = {}

    async def create_draft(self, *, text, due_ts, due_iso, doc_id, created_at) -> str:
        self._db = object()  # mark configured without touching Firestore
        reminder_id = uuid.uuid4().hex
        self.reminders[reminder_id] = {
            "text": text,
            "due_at": due_iso,
            "due_ts": due_ts,
            "status": "draft",
            "session_ref": doc_id,
            "created_at": created_at,
            "task_name": None,
        }
        return reminder_id

    async def get_status(self, reminder_id):
        d = self.reminders.get(reminder_id)
        return dict(d) if d else None

    async def set_status(self, reminder_id, status):
        if reminder_id in self.reminders:
            self.reminders[reminder_id]["status"] = status

    async def set_task(self, reminder_id, task_name):
        if reminder_id in self.reminders:
            self.reminders[reminder_id]["task_name"] = task_name

    async def mark_fired(self, reminder_id):
        """Same contract as Firestore: only scheduled→fired succeeds; a
        second call raises _AlreadyFired so idempotency is testable."""
        d = self.reminders.get(reminder_id)
        if d is None:
            raise KeyError("not found")
        if d["status"] != "scheduled":
            raise _AlreadyFired()
        d["status"] = "fired"

    async def get_task_name(self, reminder_id):
        d = self.reminders.get(reminder_id)
        return (dict(d) or {}).get("task_name") if d else None


async def _handle_create_reminder(
    args: dict[str, Any],
    reminders: ReminderStore,
    doc_id: str,
    *,
    now_s: float | None = None,
) -> dict[str, Any]:
    now_s = time.time() if now_s is None else now_s
    text = str((args or {}).get("text") or "").strip()
    if not text:
        return {"error": "empty reminder text"}
    text = _truncate(text, MAX_REMINDER_TEXT_CHARS)
    due_ts, err = _resolve_due((args or {}).get("due_at"), now_s=now_s)
    if err:
        return {"error": err}

    due_utc = datetime.fromtimestamp(due_ts, tz=timezone.utc)
    draft_id = await reminders.create_draft(
        text=text,
        due_ts=due_ts,
        due_iso=due_utc.isoformat(),
        doc_id=doc_id,
        # Same injected clock as the TTL check — one time authority per call.
        created_at=datetime.fromtimestamp(now_s, tz=timezone.utc).isoformat(),
    )
    when_human = due_utc.astimezone(_user_tz()).strftime("%A, %d %B at %I:%M %p")
    return {
        "result": (
            f"Draft ready: remind to {text} on {when_human}. "
            "Read the what and when back to the user and ask them to "
            "confirm; only after they clearly say yes call confirm_reminder "
            f"with draft_id {draft_id}."
        ),
        "draft_id": draft_id,
        "due_at_utc": due_utc.isoformat(),
        "next_step": "confirm_reminder",
    }


async def _handle_confirm_reminder(
    args: dict[str, Any],
    reminders: ReminderStore,
    scheduler: Any = None,
    *,
    now_s: float | None = None,
) -> dict[str, Any]:
    now_s = time.time() if now_s is None else now_s
    reminder_id = str((args or {}).get("reminder_id") or "").strip()
    if not reminder_id:
        return {"error": "missing reminder_id"}

    data = await reminders.get_status(reminder_id)
    if data is None:
        return {"error": f"unknown reminder '{reminder_id}'"}
    status = data.get("status")
    if status == "scheduled":
        # Idempotent double-confirm: safe to answer success again.
        return {"result": "Reminder confirmed.", "already_confirmed": True}
    if status != "draft":
        return {"error": f"reminder is not pending confirmation (status={status})"}

    # Stale-draft guard: a draft left unconfirmed past its TTL can no longer
    # be trusted to match what the user just said yes to — re-create instead.
    created_at = data.get("created_at")
    try:
        age_s = now_s - datetime.fromisoformat(created_at).timestamp()
    except (TypeError, ValueError):
        age_s = DRAFT_TTL_S + 1  # unparsable → treat as stale, fail closed
    if age_s > DRAFT_TTL_S:
        return {
            "error": (
                "this draft expired — please create the reminder again"
            ),
            "draft_id": reminder_id,
        }

    # Flip FIRST, then schedule. If scheduling fails we revert to draft so
    # the user's yes isn't silently lost without a live task behind it.
    await reminders.set_status(reminder_id, "scheduled")
    sched = scheduler if scheduler is not None else NullScheduler()
    try:
        task_name = await sched.schedule(reminder_id, float(data.get("due_ts") or 0))
        await reminders.set_task(reminder_id, task_name)
    except Exception as e:  # noqa: BLE001 — degrade gracefully, keep consent
        log.exception("scheduling failed for %s", reminder_id)
        await reminders.set_status(reminder_id, "draft")
        return {"error": "could not schedule the reminder right now"}
    scheduled = task_name is not None
    return {
        "result": (
            "Reminder confirmed."
            + ("" if scheduled else " (scheduling backend not configured)")
        ),
        "reminder_id": reminder_id,
        "task_scheduled": scheduled,
    }


async def _handle_cancel_reminder(
    args: dict[str, Any],
    reminders: ReminderStore,
    scheduler: Any = None,
) -> dict[str, Any]:
    reminder_id = str((args or {}).get("reminder_id") or "").strip()
    if not reminder_id:
        return {"error": "missing reminder_id"}
    data = await reminders.get_status(reminder_id)
    if data is None:
        return {"error": f"unknown reminder '{reminder_id}'"}
    if data.get("status") in ("fired", "cancelled"):
        return {"result": "Reminder was already closed."}
    # Best-effort task deletion BEFORE the status flip: if this fails a fire
    # may still arrive, but it hits the cancelled-status guard below.
    sched = scheduler if scheduler is not None else NullScheduler()
    task_name = data.get("task_name")
    if task_name:
        await sched.cancel(task_name)
    await reminders.set_status(reminder_id, "cancelled")
    return {"result": "Reminder cancelled."}


def _fire_allowed(data: dict[str, Any], *, now_s: float) -> str | None:
    """Shared guard for the fire path: returns an error string or None."""
    status = data.get("status")
    if status == "fired":
        return "duplicate suppressed"
    if status != "scheduled":
        return f"reminder not schedulable (status={status})"
    due_ts = data.get("due_ts")
    try:
        due_ts = float(due_ts)
    except (TypeError, ValueError):
        return "reminder has unparsable due_ts"
    if due_ts > now_s + FIRE_GRACE_S:
        return (
            f"refusing to fire early ({(due_ts - now_s) / 60:.0f} min ahead)"
        )
    return None


async def process_fired_reminder(
    reminder_id: str,
    store: ReminderStore,
    *,
    push_send: Any = None,
    now_s: float | None = None,
) -> tuple[int, dict[str, Any]]:
    """The /internal/fire-reminder core. Idempotent via the fired-status
    check inside the transactional flip; push only happens for exactly one
    caller even under concurrent Cloud Tasks retries. Returns
    (http_status, response_body).

    ``push_send(text, data) -> None`` is injected by main.py (chunk 3 wires
    FCM); with None, firing still transitions state and logs — useful for
    probes and for chunk 2 prod verification via structured logs.
    """
    now_s = time.time() if now_s is None else now_s

    async def _flip():
        data = await store.get_status_for_update(reminder_id)
        if data is None:
            return None, "not found"
        err = _fire_allowed(data, now_s=now_s)
        if err:
            return None, err
        # Firestore store enforces scheduled→fired inside a transaction; a
        # concurrent duplicate fire loses that race and lands here.
        try:
            await store.mark_fired(reminder_id)
        except _AlreadyFired:
            return None, "duplicate suppressed"
        except KeyError:
            return None, "not found"
        return data, ""

    data, err = await _flip()
    if err == "not found":
        return 404, {"error": "unknown reminder"}
    if err == "duplicate suppressed":
        return 200, {"result": "already fired"}
    if err and "not schedulable" in err:
        return 200, {"result": err}       # cancelled → swallow, 2xx stops retries
    if err and "early" in err:
        return 409, {"error": err}         # misconfigured task → let retry policy apply
    if err:
        return 400, {"error": err}

    text = (data or {}).get("text") or "your reminder"
    if push_send is not None:
        try:
            await push_send(
                text,
                {"reminder_id": reminder_id, "kind": "reminder"},
            )
        except Exception:  # noqa: BLE001 — logged; state already advanced
            log.exception("push send failed for %s", reminder_id)
    return 200, {
        "result": "fired",
        "reminder_id": reminder_id,
        "text": text[:100],
        "push_sent": push_send is not None,
    }


async def _handle_get_current_time() -> dict[str, Any]:
    """Server-authoritative clock for the model. Replaces prompt-injected
    timestamps: the prefix stays byte-identical across reconnects and can
    never go stale mid-session."""
    now_local = datetime.now().astimezone(_user_tz())
    return {
        "result": (
            now_local.strftime("It is %A, %d %B %Y, %I:%M %p")
            + f" ({now_local.tzname()})."
        ),
        "iso_utc": datetime.now(timezone.utc).isoformat(),
        "weekday": now_local.strftime("%A"),
    }


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

_reminder_store_singleton: ReminderStore | None = None


def get_reminder_store() -> ReminderStore:
    """Process-wide Firestore-backed reminder store for the REST fire path.
    (The WS tool path builds a per-connection store inside build_registry;
    both hit the same collection, so state is shared through Firestore.)"""
    global _reminder_store_singleton
    if _reminder_store_singleton is None:
        _reminder_store_singleton = ReminderStore()
    return _reminder_store_singleton


def build_registry(
    *,
    session_id: str,
    doc_id: str,
    memory: Any,
    persist_enabled: bool | None = None,
    tools_enabled: bool | None = None,
    reminders_enabled: bool | None = None,
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
    if reminders_enabled is None:
        reminders_enabled = os.environ.get("SIRIOUS_REMINDERS") == "1"
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

    # Reminders (chunk 1 + 2 of Phase 4 reminders): draft → spoken confirm →
    # schedule. Gated behind SIRIOUS_REMINDERS=1 on top of the master gate so
    # it can be rolled out independently of web_search/notes. The store is
    # Firestore when persistence is on, in-memory otherwise — same null-mode
    # pattern as notes and audit. Scheduling is Cloud Tasks when
    # SIRIOUS_TASKS_* is configured, NullScheduler (consent-only) otherwise.
    if tools_enabled and reminders_enabled:
        reminder_store = (
            ReminderStore() if persist_enabled else InMemoryReminderStore()
        )
        scheduler = _scheduler_from_env()
        registry.register(
            ToolSpec(
                name="get_current_time",
                description=(
                    "Get the current date, time and weekday in the user's "
                    "timezone. Use whenever a 'when is it now', 'what day is "
                    "it' question comes up, or before creating a reminder "
                    "when you need to anchor relative phrases."
                ),
                parameters={},
                handler=lambda args: _handle_get_current_time(),
            )
        )
        registry.register(
            ToolSpec(
                name="create_reminder",
                description=(
                    "Set a reminder for the user. STEP 1 of 2: this only "
                    "creates a DRAFT — nothing is scheduled yet. Pass "
                    "due_at as the user's own words (see due_at). Always "
                    "read the draft back (what + when) and ask them to "
                    "confirm; only after they clearly say yes, call "
                    "confirm_reminder with the returned draft_id."
                ),
                parameters={
                    "text": {
                        "type": "STRING",
                        "description": (
                            "What to remind the user about, e.g. 'call Raj'."
                        ),
                        "required": True,
                    },
                    "due_at": {
                        "type": "STRING",
                        "description": (
                            "When it should fire, as the USER'S OWN WORDS — "
                            "the server resolves them against the current "
                            "time in the user's timezone. Supported: 'in 20 "
                            "minutes', 'tomorrow 9 am', 'friday morning', "
                            "'next monday 2 pm', 'tonight', 'day after "
                            "tomorrow at 5 pm', or ISO 8601 with offset. Do "
                            "NOT compute dates yourself; pass the phrase."
                        ),
                        "required": True,
                    },
                },
                handler=lambda args: _handle_create_reminder(
                    args, reminder_store, doc_id
                ),
            )
        )
        registry.register(
            ToolSpec(
                name="confirm_reminder",
                description=(
                    "STEP 2 of 2 for setting a reminder. Call ONLY after the "
                    "user has explicitly confirmed the draft you read back. "
                    "Passes the draft_id from create_reminder; scheduling "
                    "happens here."
                ),
                parameters={
                    "reminder_id": {
                        "type": "STRING",
                        "description": "The draft_id returned by create_reminder.",
                        "required": True,
                    },
                },
                requires_confirmation=False,  # this call IS the confirmation step
                handler=lambda args: _handle_confirm_reminder(
                    args, reminder_store, scheduler
                ),
            )
        )
        registry.register(
            ToolSpec(
                name="cancel_reminder",
                description=(
                    "Cancel a pending or scheduled reminder by its id when the "
                    "user asks to stop it."
                ),
                parameters={
                    "reminder_id": {
                        "type": "STRING",
                        "description": "The reminder id to cancel.",
                        "required": True,
                    },
                },
                handler=lambda args: _handle_cancel_reminder(
                    args, reminder_store, scheduler
                ),
            )
        )

    return registry, audit
