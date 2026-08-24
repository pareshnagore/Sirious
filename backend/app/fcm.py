"""FCM push delivery for reminders (Phase 4 chunk 3).

Design notes
------------
- No firebase-admin dependency: the Admin SDK's only real job here is
  minting an OAuth2 access token for the FCM HTTP v1 API, which
  google.auth (already installed) does directly against Application
  Default Credentials. On Cloud Run that is the service's runtime SA.
- Device tokens live in Firestore ``device_tokens/{token_hash}``:
    { token, platform, registered_at, last_seen_at }
  Doc ID = SHA256(token) so a re-registration overwrites instead of
  duplicating and an unregistered-token cleanup is one delete.
- FCM v1 semantics used by send_push():
    404 / UNREGISTERED  → token dead (app uninstalled etc.) → caller
                          removes the device doc;
    429 / 5xx           → transient, safe to skip (next reminder retries);
    200                 → delivered to FCM (delivery to the DEVICE is
                          Android's problem once we ship chunk 4).
- Every failure is logged via log_event-style structured prints and never
  propagates: a broken push must not turn a fired reminder into a 5xx
  (that would trigger Cloud Tasks retries for a reminder already fired).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("sirious.fcm")

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
DEVICE_TOKENS_COLLECTION = "device_tokens"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeviceTokenStore:
    """Firestore-backed registry of FCM device tokens."""

    def __init__(self) -> None:
        self._db: Any = None

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

    @staticmethod
    def _doc_id(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def register(self, token: str, platform: str | None = None) -> str:
        db = self._ensure_db()
        doc_id = self._doc_id(token)
        await (
            db.collection(DEVICE_TOKENS_COLLECTION)
            .document(doc_id)
            .set(
                {
                    "token": token,
                    "platform": platform or "android",
                    "registered_at": _now_iso(),
                    "last_seen_at": _now_iso(),
                },
                merge=True,
            )
        )
        return doc_id

    async def remove(self, token: str) -> None:
        db = self._ensure_db()
        await (
            db.collection(DEVICE_TOKENS_COLLECTION)
            .document(self._doc_id(token))
            .delete()
        )

    async def list_all(self, limit: int = 50) -> list[dict[str, Any]]:
        """Active registrations. Client-side cap: personal scale."""
        snaps = (
            self._ensure_db()
            .collection(DEVICE_TOKENS_COLLECTION)
            .limit(limit)
            .stream()
        )
        out = []
        async for s in snaps:
            d = s.to_dict() or {}
            d["id"] = s.id
            out.append(d)
        return out


def _get_access_token_cached(cache: dict[str, Any]) -> str:
    """ADC-based OAuth2 token for FCM HTTP v1, cached until near-expiry."""
    creds = cache.get("creds")
    now = time.time()
    tok = cache.get("token")
    exp = cache.get("exp", 0)
    if creds is None or now > exp - 60:
        import google.auth

        creds, _ = google.auth.default(scopes=[FCM_SCOPE])
        from google.auth.transport.requests import Request

        request = Request()
        creds.refresh(request)
        cache["creds"] = creds
        cache["token"] = creds.token
        cache["exp"] = now + 3600
        return creds.token
    return tok


def fcm_send_url() -> str:
    project = os.environ.get("GCP_PROJECT") or os.environ.get(
        "GOOGLE_CLOUD_PROJECT"
    ) or os.environ.get("SIRIOUS_FCM_PROJECT") or "sirious-2026"
    return FCM_SEND_URL.format(project=project)


def send_push(
    device_token: str,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
    *,
    token_cache: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """One FCM HTTP v1 send. Synchronous on purpose — called inside
    asyncio.to_thread from the fire path (blocking urllib keeps us off any
    event loop and avoids pulling httpx config into this module).

    Returns (delivered, status): status is 'ok' | 'unregistered' | error text.
    """
    cache = token_cache if token_cache is not None else {}
    payload = {
        "message": {
            "token": device_token,
            "notification": {"title": title, "body": body},
            "data": {k: str(v) for k, v in (data or {}).items()},
        }
    }
    req = urllib.request.Request(
        fcm_send_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_get_access_token_cached(cache)}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                return True, "ok"
            return False, f"http_{resp.status}"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        if e.code == 404 or "UNREGISTERED" in detail:
            return False, "unregistered"
        if e.code == 400 and "not a valid FCM registration token" in detail:
            # Malformed/garbage token (bad registration, not a real device).
            # Treat like unregistered so the registry self-cleans.
            return False, "unregistered"
        log.warning("FCM send failed %s: %s", e.code, detail)
        return False, f"http_{e.code}"
    except Exception as e:  # noqa: BLE001 — network layer
        log.warning("FCM send error: %r", e)
        return False, f"error:{e!r}"


async def deliver_reminder_to_all_devices(
    text: str,
    reminder_id: str,
    tokens: DeviceTokenStore,
    *,
    max_devices: int = 8,
) -> dict[str, int]:
    """Fire-path helper: push to every registered device, prune dead tokens.
    Never raises. Returns counts for structured logging."""
    devices = await tokens.list_all(limit=max_devices)
    sent = failed = pruned = 0
    cache: dict[str, Any] = {}
    title = "Reminder"
    body = text[:300]
    for dev in devices:
        token = dev.get("token") or ""
        if not token:
            continue
        ok, status = await __import__("asyncio").to_thread(
            send_push, token, title, body,
            {"reminder_id": reminder_id, "kind": "reminder"},
            token_cache=cache,
        )
        if ok:
            sent += 1
        elif status == "unregistered":
            await tokens.remove(token)
            pruned += 1
        else:
            failed += 1
    return {"devices": len(devices), "sent": sent, "failed": failed,
            "pruned": pruned}
