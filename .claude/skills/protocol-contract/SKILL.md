---
name: protocol-contract
description: Verify backend ↔ mobile WebSocket protocol conformance after either side changes. Reads the contract, cross-checks server emission/parsing in backend/app/main.py against client handling in mobile/lib, and flags drift. Use whenever editing streaming, barge-in, audio, or session-lifecycle code.
user-invocable: false
---

# Protocol Contract Check

Maintains the single source of truth for interop between the FastAPI backend and the Flutter client. Run **before and after** editing any WebSocket / realtime code.

## The authoritative spec

Read **`backend/docs/websocket_protocol.md`** (protocol v1). It is the contract.

## Files that must stay in sync

| Concern | Backend (server) | Mobile (client) |
|---------|------------------|-----------------|
| Connection lifecycle | `backend/app/main.py` — `/ws`, `session_started`, end-of-session handling | `mobile/lib/services/websocket_client.dart`, `sirious_session_controller.dart` |
| Audio frames (binary PCM) | forwards mic PCM → Gemini; relays assistant PCM | `audio_capture_service.dart` (16 kHz in), `audio_playback_service.dart` (24 kHz out) |
| Control / JSON text frames | `stop`, `ping` handling, transcript events | `websocket_client.dart` frame dispatch |
| Barge-in / interruption | `finish_turn("interrupted")` lifecycle | `sirious_session_controller.dart` flush logic |

## Audit checklist

For each change, verify **both directions match the spec**:

1. **Frame type** — the code sends/expects `bytes` for audio and `str`/JSON for control on the correct side.
2. **Audio encoding** — mic input must be int16 LE mono `16 kHz`; assistant output int16 LE mono `24 kHz`, variable chunk sizes.
3. **Lifecycle** — server sends `session_started`; client creates a session on it. Client sends `stop` to end; server need not send `session_ended` (it closes the socket).
4. **JSON event payloads** — every server→client event the client `jsonDecode`s must carry at least the keys the client reads. Cross-check server `log_event(...)`/`send_json(...)` payloads against every client read.
5. **No phantom contract** — if one side now emits/handles a field the other never sent, that is drift even if both "work" today.

## When the contract is violated

1. Print the specific mismatch (side, message, missing/extra field).
2. Fix the **producer** to match the documented spec, not the consumer to match a bug.
3. Update **`backend/docs/websocket_protocol.md`** if the spec itself is what changed (the doc's header notes the version/date it was last aligned).
4. Re-run the audit. Do not claim conformance until the checklist passes.

## Guardrails

- Do not edit the protocol doc as a side effect of refactoring — it changes only when server behavior intentionally changes.
- If the client and server are both wrong but agree, it is still drift from the spec: say so.