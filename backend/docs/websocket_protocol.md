# Sirious WebSocket Protocol

**Protocol version:** `1`  
**Last aligned with:** `backend/app/main.py` (22 August 2026 — Phase 2 auth + history)

This document is the contract between the Sirious server and clients (Flutter, Python test clients, etc.). It reflects **actual server behavior**, not a planned API.

---

## Overview

```text
Client                          Cloud Run (FastAPI)              Gemini Live
  │                                    │                              │
  │──── WebSocket connect /ws ────────►│                              │
  │                                    │──── live.connect() ─────────►│
  │◄─── JSON session_started ──────────│                              │
  │                                    │                              │
  │──── binary PCM (mic) ─────────────►│──── send_realtime_input() ──►│
  │                                    │                              │
  │◄─── binary PCM (assistant) ────────│◄── model audio ──────────────│
  │◄─── JSON events ───────────────────│◄── transcripts / lifecycle ──│
  │                                    │                              │
  │──── text "stop" or disconnect ────►│                              │
```

Each WebSocket connection creates one server `session_id`, one Gemini Live session, and one bidirectional audio/event stream.

---

## Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/ws` | WebSocket | Real-time voice session |
| `/health` | GET | Liveness check → `{"status": "ok"}` (no auth) |
| `/sessions` | GET | Session history list, newest first (**auth**) |
| `/sessions/{id}` | GET | Full transcript of one session (**auth**) |

### Production URL (current deployment)

```text
wss://sirious-api-635321277027.asia-south1.run.app/ws
https://sirious-api-635321277027.asia-south1.run.app/sessions
https://sirious-api-635321277027.asia-south1.run.app/health
```

Region: `asia-south1` · GCP project: `sirious-2026`

---

## Authentication (Phase 2)

When the server has `SIRIOUS_AUTH_TOKEN` set (always true in production), every
endpoint requires a static bearer token:

- **REST** — header `Authorization: Bearer <token>`; missing/wrong token → `401`.
- **WebSocket** — query parameter `/ws?token=<token>`; missing/wrong token is
  rejected at the **handshake**, before `accept()`, so no Gemini session is ever
  opened for an unauthenticated peer. Clients see the HTTP upgrade fail
  (`401`/`403`).

The mobile client stores the token in `flutter_secure_storage` (Android
Keystore-backed) and appends it to both the REST calls and the WS URL.
`/health` stays open for liveness probes. If `SIRIOUS_AUTH_TOKEN` is unset
(local dev), all endpoints are open.

---

## Connection lifecycle

1. Client opens WebSocket to `/ws`.
2. Server accepts, generates a `session_id`, connects to Gemini Live, and sends `session_started`.
3. Client may immediately begin sending microphone PCM (no start handshake required).
4. Server forwards audio to Gemini and relays assistant audio + JSON events back.
5. Session ends when:
   - client sends text `"stop"`, or
   - client disconnects, or
   - an unrecoverable server/Gemini error occurs.

**Note:** The server does **not** send a `session_ended` JSON message to the client. On disconnect, the WebSocket closes and the server logs `session_ended` internally.

---

## Message types

WebSocket frames are either **binary** (audio) or **text** (control on client→server; JSON on server→client).

| Direction | Frame type | Content |
|-----------|------------|---------|
| Client → Server | Binary | Microphone PCM |
| Client → Server | Text | Control commands (`stop`, `ping`) |
| Server → Client | Binary | Assistant PCM |
| Server → Client | Text (JSON) | Transcripts, lifecycle, errors |

Clients **must** distinguish binary vs text on every received frame.

---

## Audio formats

### Client → Server (microphone input)

| Property | Value |
|----------|-------|
| Encoding | PCM signed 16-bit little-endian (`int16`) |
| Sample rate | 16 000 Hz |
| Channels | 1 (mono) |
| MIME (server→Gemini) | `audio/pcm;rate=16000` |

Recommended chunk duration: **20–100 ms** per binary frame.

Example at 100 ms:

```text
samples per chunk = 16000 × 0.1 = 1600
bytes per chunk   = 1600 × 2 = 3200
```

The server forwards each binary frame to Gemini as it arrives. Send continuously while the session is active.

### Server → Client (assistant output)

| Property | Value |
|----------|-------|
| Encoding | PCM signed 16-bit little-endian (`int16`) |
| Sample rate | 24 000 Hz |
| Channels | 1 (mono) |

Binary frames may arrive in variable sizes. Clients should queue and play sequentially.

---

## Client → Server

### Microphone audio

Send raw PCM bytes as a **binary WebSocket frame**. No JSON wrapper.

```text
[3200 bytes of int16 mono PCM @ 16 kHz]
```

### Control commands

Send as **plain text** WebSocket frames (not JSON).

| Command | Behavior |
|---------|----------|
| `stop` | Server stops reading from client and ends the session loop |
| `ping` | Server replies with JSON `{"type": "pong"}` |

Example:

```text
stop
```

```text
ping
```

**Not implemented:** JSON control messages such as `{"type": "session.start"}` or `{"type": "session.stop"}`. Do not send these unless the server is updated to accept them.

---

## Server → Client

All non-audio messages are JSON text frames with a `type` field.

### `session_started`

Sent once after Gemini Live connects.

```json
{
  "type": "session_started",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "resumed": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string (UUID) | Server-assigned ID for THIS connection (changes on every reconnect) |
| `resumed` | bool (**v2**) | true when the Gemini conversation context was restored from a previous connection — see [Session resumption](#session-resumption-protocol-v2) below |

---

## Session resumption (protocol v2)

Keeps the **Gemini model's memory** alive across network blips. Without it,
every reconnect starts a fresh Gemini session: the on-screen transcript is
preserved client-side, but the model forgets everything said before the drop.

### How it works

1. The client generates a stable `client_session_id` when the user starts a
   session and sends it as a WebSocket **query parameter** on every connect:
   `/ws?client_session_id=<id>`. It reuses the same id across reconnects and
   clears it only when the user ends the session.
2. The backend enables `session_resumption` on its Gemini Live connection and
   stores every resumable handle it receives, keyed by `client_session_id`
   (in-memory, 2 h TTL — matching Gemini's handle validity).
3. On reconnect with a known id + live handle, the backend passes the handle
   to Gemini (`SessionResumptionConfig.handle`) → the SAME Gemini session is
   resumed and `session_started.resumed=true` is sent. Unknown/expired id →
   fresh session (`resumed=false`). A clean `stop` from the client deletes the
   stored handle, so the next Start begins genuinely fresh.
4. Fallback is automatic: if no valid handle exists (expired, instance
   recycled, or the model never issued one), the reconnect behaves like the
   v1 flow.

### Model requirement (IMPORTANT, verified 21 Aug 2026)

Resumption only works on models that actually emit **resumable** handles:

| Model | Resumable handles |
|---|---|
| `gemini-2.5-flash-native-audio-preview-12-2025` | ❌ never (only a non-resumable setup update) |
| `gemini-3.1-flash-live-preview` | ✅ yes (arrives right after the first turn completes) |

The backend reads `SIRIOUS_MODEL` (env var) so this can be switched at deploy
time without a code change. Default remains the 2.5 native-audio preview until
the model upgrade is done deliberately.

### Client UX

On reconnect, the on-screen log shows which path was taken:
- `Reconnected — Gemini context RESUMED (after N retries)` — memory intact
- `Reconnected after N retries (fresh Gemini context)` — v1 fallback

---

### `user_transcript`

Streaming fragment of user speech transcription from Gemini.

```json
{
  "type": "user_transcript",
  "text": "tell me about"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `text` | string | Incremental fragment — **not** a full sentence |

**Client handling:** Append fragments to build the current user utterance. Do not display each fragment as a separate final line unless intentionally showing live captions.

---

### `assistant_transcript`

Streaming fragment of assistant speech transcription.

```json
{
  "type": "assistant_transcript",
  "text": "India is"
}
```

Same fragment semantics as `user_transcript`.

---

### `response_finished`

Model finished generating the current response (Gemini `generation_complete`).

```json
{
  "type": "response_finished"
}
```

**Important:** Audio already sent may still be buffered on the client. This event means generation stopped, not necessarily that playback finished.

---

### `interrupted`

User barged in while the assistant was speaking. Gemini cancelled the in-progress response.

```json
{
  "type": "interrupted"
}
```

**Client handling (required for good UX):**

1. Clear the playback audio queue.
2. Flush/stop the audio output device.
3. Resume accepting new assistant audio for the next response.

See `backend/test_continuous.py` for a reference implementation.

---

### `turn_complete`

Conversational turn finished normally (Gemini `turn_complete`).

```json
{
  "type": "turn_complete"
}
```

After this, the server resets turn state internally. A new user utterance starts a new turn.

---

### `session_warning`

Gemini signaled an upcoming session lifecycle event (typically `go_away` before ~8-minute limit).

```json
{
  "type": "session_warning",
  "code": "GO_AWAY",
  "time_left": "50s"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `code` | string | Currently `"GO_AWAY"` |
| `time_left` | string | Human-readable time until Gemini session ends |

Automatic server-side resumption **is implemented (protocol v2)** — see
[Session resumption](#session-resumption-protocol-v2). On `go_away` the client
may proactively reconnect with its `client_session_id` to continue the same
Gemini conversation on a fresh connection.

---

### `session_resumption`

Server received a resumable session handle from Gemini (for future reconnect).

```json
{
  "type": "session_resumption",
  "handle": "<opaque-handle>"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `handle` | string | Opaque Gemini resumption token |

**v2:** the backend stores this handle server-side keyed by the client's
`client_session_id` and uses it automatically on reconnect — clients do not
need to store or return it. See [Session resumption](#session-resumption-protocol-v2).

---

### `pong`

Reply to client `ping`.

```json
{
  "type": "pong"
}
```

---

### `error`

```json
{
  "type": "error",
  "code": "GEMINI_ERROR",
  "message": "Human-readable description"
}
```

| Code | When |
|------|------|
| `CLIENT_ERROR` | Exception while processing client input |
| `GEMINI_ERROR` | Exception in Gemini receive loop |

The session may end after an error. Clients should show the message and return to idle.

### `session_recovering` (protocol v2, Phase 6 step 5)

```json
{
  "type": "session_recovering"
}
```

Sent when the **Gemini leg** dies mid-conversation while the client leg is
still healthy (e.g. Gemini closes the session after rapid barge-ins —
"1007 Precondition check failed", seen in prod 31 Aug 2026). Immediately
after this event the server closes the socket with close code **4402**
(`CLOSE_RECOVER`, app-defined). The client's normal network-blip
auto-reconnect then takes over: reconnecting with the SAME
`client_session_id` resumes the SAME Gemini conversation via protocol-v2
resumption (handle was stored on every update). To the user this appears
as a brief "reconnecting…" flash — no conversation loss, no action needed.

| Close code | Meaning | Client action |
|------|---------|---------------|
| `4402 CLOSE_RECOVER` | Gemini leg lost; conversation resumable | Auto-reconnect (blip path); do NOT reset UI state |

---

### Assistant audio

Raw PCM bytes as a **binary WebSocket frame** (24 kHz mono int16). No JSON metadata per chunk.

---

## Turn lifecycle

A **turn** is one user→assistant exchange. The server tracks turns internally and logs `turn_summary` to stdout (not sent over WebSocket).

### Typical successful turn

```text
[user speaks]
  → user_transcript (fragments)
  → assistant_transcript (fragments)
  → binary audio chunks
  → response_finished
  → turn_complete
```

### Interrupted turn

```text
[user speaks]
  → user_transcript (fragments)
  → assistant_transcript (fragments)
  → binary audio chunks
  → response_finished        (may occur before interrupted)
  → interrupted
  → [new turn begins]
  → user_transcript (new utterance)
  → ...
```

### Event semantics

| Event | Meaning |
|-------|---------|
| `response_finished` | Model stopped generating current answer |
| `interrupted` | User cut off the assistant; discard stale playback |
| `turn_complete` | Turn closed cleanly |

`interrupted` and `turn_complete` both end the server-side turn and emit an internal `turn_summary` log with `reason` of `"interrupted"` or `"turn_complete"`.

If the WebSocket drops mid-turn, the server logs `turn_summary` with `reason: "session_ended"`.

---

## Recommended client architecture

Decouple network I/O from audio playback.

```text
                    ┌───────────────┐
                    │  WebSocket    │
                    │  receiver     │
                    └───────┬───────┘
                            │
              ┌─────────────┴─────────────┐
              │                           │
           binary                      JSON
              │                           │
              ▼                           ▼
       Playback queue              Event handler
              │                           │
              ▼                           │
       Audio output ◄──── on interrupted ─┘
                         clear queue + flush
```

Minimum client tasks:

1. **Mic sender** — capture PCM, send binary frames.
2. **Receiver** — route bytes to playback queue, JSON to event handler.
3. **Speaker worker** — drain playback queue to audio output.

---

## Client state machine (recommended)

```text
IDLE
  ↓ connect
CONNECTING
  ↓ session_started
LISTENING
  ↓ assistant responding
RESPONDING / PLAYING
  ↓ turn_complete
LISTENING

PLAYING
  ↓ user speaks (barge-in)
INTERRUPTING   ← on `interrupted`: flush audio
  ↓
LISTENING
```

---

## Server-side logging (not over WebSocket)

The server emits structured JSON logs to stdout for debugging and future persistence. Clients do not receive these directly.

| Log event | Description |
|-----------|-------------|
| `session_started` | WebSocket session opened |
| `turn_started` | New turn began |
| `user_transcript_fragment` | User text fragment |
| `assistant_transcript_fragment` | Assistant text fragment |
| `generation_complete` | Model generation finished |
| `interrupted` | Barge-in detected |
| `turn_complete` | Turn finished |
| `turn_summary` | Full turn record (user/assistant text, bytes, flags) |
| `go_away` | Gemini session winding down |
| `session_resumption` | Resumption handle received |
| `client_disconnected` | Client closed connection |
| `session_ended` | Session cleanup complete |

Example `turn_summary` (stdout only):

```json
{
  "timestamp": "2026-08-16T10:00:00+00:00",
  "session_id": "...",
  "event": "turn_summary",
  "turn_id": "...",
  "reason": "interrupted",
  "user_text": "Wait, tell me about Mumbai instead.",
  "assistant_text": "India is a very large country...",
  "audio_in_bytes": 128000,
  "audio_out_bytes": 96000,
  "generation_complete": true,
  "turn_complete": false,
  "interrupted": true
}
```

---

## Reference client

`backend/test_continuous.py` implements protocol v1:

- 16 kHz mic → binary send
- Binary recv → asyncio playback queue
- JSON `interrupted` → queue clear + audio stream reset
- Transcript fragment printing (diagnostic only)

---

## Future protocol changes (not implemented)

Planned improvements may add:

- `protocol_version` field in `session_started`
- JSON control messages (`session.stop`) instead of plain text
- `session_ended` event to client
- `turn_id` on transcript events for client correlation

~~Automatic session resumption without client action~~ → **done in protocol v2**
(see [Session resumption](#session-resumption-protocol-v2)).

~~Persistent session history + REST readback~~ → **done in Phase 2** (below).

Clients should ignore unknown JSON fields and unknown `type` values for forward compatibility.

---

## Session history REST API (Phase 2)

Every conversation is persisted to Firestore (one document per **logical**
conversation — the doc id equals the client's `client_session_id`, so a
resuming reconnect extends the same document). Writes are turn-level via an
async writer; nothing in the voice hot path blocks on Firestore, and a
disconnect flushes the final state. All endpoints require
`Authorization: Bearer <token>`.

### `GET /sessions?limit=50`

Newest-first list (ordered by last update).

```json
{
  "sessions": [
    {
      "id": "b1a0…",
      "title": "what did we discuss about Mumbai?",
      "preview": "Mumbai is the capital of…",
      "started_at": "2026-08-22T03:30:00+00:00",
      "ended_at": "2026-08-22T03:36:12+00:00",
      "duration_s": 372.4,
      "turn_count": 9,
      "model": "gemini-3.1-flash-live-preview",
      "updated_ms": 1787368000000
    }
  ]
}
```

`title` is the first user utterance (truncated to 80 chars), `preview` is the
tail of the last assistant reply.

### `GET /sessions/{id}`

```json
{
  "id": "b1a0…",
  "title": "…",
  "model": "…",
  "device": "<client User-Agent>",
  "started_at": "…",
  "ended_at": "…",
  "duration_s": 372.4,
  "end_reason": "session_ended",
  "resume_count": 1,
  "turn_count": 9,
  "turns": [
    {
      "id": "turn-uuid",
      "started_at": "…",
      "ended_at": "…",
      "user_text": "full user utterance",
      "assistant_text": "full assistant utterance",
      "interrupted": false,
      "reason": "turn_complete"
    }
  ]
}
```

Errors: `401` no/ bad token · `404` unknown id · `503` store temporarily
unavailable.

**Data model note:** `resume_count > 0` marks sessions that survived network
blips; turns carry an `interrupted` flag from barge-ins. Audio bytes are
persisted as counters only — transcripts are text-first (audio archival is out
of scope until a later phase).

### Transcript-replay fallback (Phase 2, verified live 22 Aug)

When a client reconnects with a `client_session_id` that has **no live
resumption handle** (clean end dropped it, 2 h expiry passed, or the instance
was recycled), the backend fetches that conversation's recent turns (last 12)
from Firestore and injects them into the Gemini `system_instruction` before
connecting. Result: conversational memory survives even without native
session resumption. The lookup happens once at connect time and is
best-effort — on failure the session simply starts fresh. Verified live:
fact stated in conversation 1 → clean stop → brand-new session asked about
the fact → answered correctly from replay.

### Memory API (Phase 3)

Long-term memory is extracted from every session after it ends (one flash-model
call per session, embeddings per memory; gated by `SIRIOUS_MEMORY=1`). Memories
live in the Firestore `memories` collection with provenance
(`session_ref` + `turn_ids` + date), topics, and entities. At each new WS
connect a bounded block of memories (top facts/tasks + recent episodic index)
is injected into `system_instruction`.

#### `GET /memories`

- No query → newest-first list: `{ "memories": [ … ] }`
- `GET /memories?q=<text>` → semantic search, cosine-ranked hits:

```json
{
  "query": "birds",
  "memories": [
    {
      "id": "…",
      "type": "episodic",
      "text": "User and assistant discussed peacock colors",
      "topics": ["birds", "peacocks", "colors"],
      "entities": [],
      "score": 0.71,
      "provenance": [
        {"session_ref": "recall-peacock-ab12", "started_at": "…", "title": "…"}
      ]
    }
  ]
}
```

#### `DELETE /memories/{id}`

Soft-delete (`deleted: true` — the doc remains for audit but never resurfaces).
Returns `{"deleted": true, "id": "…"}`, or `404` for unknown ids.

Errors: `401` no/ bad token · `503` memory store unavailable.

### Agentic memory search (Phase 3, live)

When `SIRIOUS_MEMORY=1`, every Live session carries a
`search_past_conversations` function declaration. When the model judges a
question reaches beyond its injected context ("did we ever talk about X?"),
it calls the tool; the server embeds the query, cosine-ranks ALL active
memories, and returns the top-5 with text, type, score, date and
session_ref. The MODEL decides relevance from the scores (weak matches on
unrelated topics get honestly dismissed). Round-trip visible in structured
logs as `tool_called` / `tool_result`.

### Session deletion (Phase 3 add-on)

`DELETE /sessions/{id}` removes the conversation document AND cascades into
memory: every provenance entry citing it is stripped; memories left with no
sources are hard-deleted; the extraction watermark is dropped. Mobile:
swipe-to-delete on History rows with confirmation. Returns
`{"deleted": true, "id": "…", "memories": {"memories_updated": N,
"memories_deleted": M}}`.

---

## Quick reference

### Client sends

| Content | Format |
|---------|--------|
| Microphone | Binary PCM 16 kHz mono int16 |
| Stop session | Text: `stop` |
| Keepalive | Text: `ping` |

### Server sends

| Content | Format |
|---------|--------|
| Assistant voice | Binary PCM 24 kHz mono int16 |
| Session ready | JSON: `session_started` |
| User speech text | JSON: `user_transcript` |
| Assistant speech text | JSON: `assistant_transcript` |
| Generation done | JSON: `response_finished` |
| Barge-in | JSON: `interrupted` |
| Turn done | JSON: `turn_complete` |
| Session expiry warning | JSON: `session_warning` |
| Resumption handle | JSON: `session_resumption` |
| Ping reply | JSON: `pong` |
| Failure | JSON: `error` |
