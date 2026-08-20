---
name: realtime-session-test
description: Run a live end-to-end WebSocket voice session against the Sirious backend (local uvicorn or deployed URL) and report round-trip health — turns, bytes, output-audio duration, events. Use when the user asks to test a session, verify the backend, or check latency/audio flow.
disable-model-invocation: true
---

# Realtime Session Test

Tests the full Sirious audio path: **FastAPI backend ↔ Gemini Live Audio ↔ WebSocket**, returning objective session metrics instead of a manual smoke test.

## Ground truths (do not re-derive)

- Backend entrypoint: `backend/app/main.py`, app served by `uvicorn app.main:app`.
- Protocol: `backend/docs/websocket_protocol.md` (v1).
- Audio contract:
  - **Client → server (mic):** PCM signed 16-bit LE, mono, **16 kHz**.
  - **Server → client (assistant):** PCM signed 16-bit LE, mono, **24 kHz**.
  - Binary frames are raw PCM (no JSON wrapper). Text frames are control/JSON.
- Backend needs `GEMINI_API_KEY` on the server. **If target is local uvicorn, you must confirm the key is available** (e.g. `GEMINI_API_KEY` from the user's env/shell, since it is not in `.env`).
- Existing ad-hoc scripts (`backend/test_ws.py`, `test_continuous.py`, `test_gemini_live.py`) hit the **production** URL and need an input mic + `sounddevice`. Prefer the bundled `run_smoke.py` below for a deterministic check.

## Two run modes

### A. Production (default, no mic needed)
Point at the documented deployed endpoint (see protocol doc). Use the bundled `run_smoke.py` — it synthesizes a short input buffer and validates the returned audio.

### B. Local backend
1. Start backend: `cd backend && GEMINI_API_KEY=… uvicorn app.main:app --port 8080` (use the user's key).
2. Run the same smoke script against `ws://localhost:8080/ws`.
3. **Stop the server when done** so you don't leave a dangling process.

## Bundled runner: `run_smoke.py`

Works against both `ws://` (local) and `wss://` (production). Needs only `websockets` (already present in the backend venv — uvicorn depends on it).

```bash
# Local backend (TLS not required)
cd backend && .venv/bin/python ../.claude/skills/realtime-session-test/run_smoke.py \
  --ws ws://localhost:8080/ws

# Production (TLS)
cd backend && .venv/bin/python ../.claude/skills/realtime-session-test/run_smoke.py \
  --ws wss://sirious-api-635321277027.asia-south1.run.app/ws --timeout 30
```

The script:
1. Connects to `/ws`, generates a throwaway `session_id`-free session.
2. Streams ~0.5 s of synthesized 16 kHz int16 PCM (a short burst), then sends text `"stop"`.
3. Collects every frame for `--timeout` s, classifying binary vs text.
4. Prints a JSON summary: `{binary_frames, text_frames, audio_bytes, audio_seconds@24k, first_text_event}` and saves the assistant audio to `smoke_response.wav` in the repo root (24 kHz mono).

**Validate success** (all must hold):
- `audio_bytes > 0` — server returned assistant audio.
- `binary_frames > 0` — audio frames flowed back.
- No premature websocket close before `stop` was processed.

**Report** to the user as concise bullets: bytes received, output duration, number of events, and the target (local vs prod). If audio is empty or the socket closes early, say so plainly and describe the failure (don't claim success).

## Latency / barge-in check (needs a real mic)
Offer this only if the user wants deeper signal. Requires `pip install websockets sounddevice numpy` in `backend/.venv`, then run `backend/test_continuous.py` (interactive, streams live mic) and summarize its `EVENT`/latency output.

## Guardrails
- **Live session = real Gemini API cost.** Only run when asked, never proactively.
- Use the **production URL sparingly**; prefer a local backend loop for iteration.
- Never print or reuse API keys; read them from the environment only.
- Do not modify production config or `.env` while testing.