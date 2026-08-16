# Sirious WebSocket Protocol

**Protocol version:** `1`  
**Last aligned with:** `backend/app/main.py` (16 August 2026)

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
| `/health` | GET | Liveness check → `{"status": "ok"}` |

### Production URL (current deployment)

```text
wss://sirious-api-635321277027.asia-south1.run.app/ws
https://sirious-api-635321277027.asia-south1.run.app/health
```

Region: `asia-south1` · GCP project: `sirious-2026`

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
  "session_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string (UUID) | Server-assigned ID for logs and future persistence |

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

Automatic server-side resumption is **not** implemented yet. Clients may log this and prepare for reconnect in a future protocol version.

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

No automatic resume is performed today. Store only if implementing client-driven reconnection later.

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
- Automatic session resumption without client action

Clients should ignore unknown JSON fields and unknown `type` values for forward compatibility.

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
