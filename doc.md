# Sirious — Vision, History & Handoff Notes

**Origin:** summary of the project-setup conversation (with ChatGPT), 16 Aug 2026.
**Last revised:** 21 Aug 2026 (brought up to date after Phase 1 closure).

> **How to use this document** — three docs serve different purposes:
>
> | Doc | Trust it for |
> |---|---|
> | `doc.md` (this file) | Project vision, why the architecture is shaped this way, history, hard-won lessons |
> | `product_phases.md` | **Living status tracker** — phase definitions, checklists, current progress. Source of truth for "where are we" |
> | `backend/docs/websocket_protocol.md` | **Authoritative wire protocol** (v2) between client and server |
>
> If this file ever disagrees with those two, believe them — this file is the story, they are the state.

---

# 1. Broad-level goal

The project is called **Sirious**.

The broad goal is to build a **personal conversational AI assistant** that behaves more like a persistent, always-available personal agent than a conventional chatbot.

The intended experience is:

* Primarily **voice-based**.
* User can speak naturally rather than press buttons for every interaction.
* Assistant listens continuously during a conversation.
* User can **interrupt the assistant naturally while it is speaking**.
* Assistant should stop its current response and immediately respond to the new user input.
* Conversation should eventually be permanently recorded/transcribed.
* The system should maintain useful **memory/context about people, conversations and the user's preferences**.
* Long-term, the assistant should be able to take actions/use tools rather than merely answer questions.
* It should work across **laptop and mobile**, with mobile eventually being the primary interface.
* The mobile client should be relatively lightweight; cloud infrastructure should handle the heavy AI processing.

Everything built so far serves the first rung of that ladder: a reliable, low-latency,
interruptible real-time voice loop. Memory, tools, and agency come later, in layers.

---

# 2. Current architecture

```text
┌─────────────────────────────┐
│  Sirious mobile client      │
│  (Flutter, Android)         │
│  mic 16 kHz PCM ──► WS ──►  │
│  ◄── WS ── speaker 24 kHz   │
└──────────────┬──────────────┘
               │ WebSocket (binary PCM + JSON events)
               ▼
┌─────────────────────────────┐
│  Cloud Run (asia-south1)    │
│  FastAPI /ws  (main.py)     │
│  thin real-time bridge      │
└──────────────┬──────────────┘
               │ Gemini Live API
               ▼
┌─────────────────────────────┐
│  Gemini Live                │
│  SIRIOUS_MODEL env var:     │
│  gemini-3.1-flash-live-     │
│  preview (prod since        │
│  21 Aug; default in code    │
│  is 2.5-flash-native-audio) │
└─────────────────────────────┘
```

Endpoints:

```text
wss://sirious-api-635321277027.asia-south1.run.app/ws     (voice)
https://sirious-api-635321277027.asia-south1.run.app/health
GCP project: sirious-2026        region: asia-south1
```

Technology:

* **Client** — Flutter/Dart (`mobile/`); `web_socket_channel`, `record`, `flutter_pcm_sound`, `permission_handler`.
* **Server** — Python FastAPI + Uvicorn + google-genai SDK (pinned `==2.19.0`) on Cloud Run; source-deployed via Cloud Build (`gcloud run deploy sirious-api --source backend --region asia-south1`).
* **Model selection** — `SIRIOUS_MODEL` env var on Cloud Run. Set to `gemini-3.1-flash-live-preview` on 21 Aug because it is the only Live model tried that emits **resumable session handles** (the 2.5-native-audio model never does — verified by direct probe). Audio pricing is identical between them.

---

# 3. Why this architecture was selected

The key decision: use **Gemini Live directly for the conversational voice loop** instead of assembling speech-to-text → LLM → text-to-speech as three separate services.

The Live API provides, in one session: streaming audio in/out, input/output transcription, conversational state, interruption/barge-in semantics, and turn detection. That is much closer to the intended Sirious experience than any stitched pipeline.

The Cloud Run server stays a **thin real-time bridge** (client WebSocket ⇄ FastAPI ⇄ Gemini Live session) with no business logic in the audio path. Later capabilities attach around it without touching transport:

```text
                    ┌── Memory
                    ├── Tools
                    ├── Web search
Client → Live layer ─┼── Personal context
                    ├── Long-term transcript
                    └── Action execution
```

---

# 4. Audio format (stable contract)

* **Client → server:** PCM signed 16-bit LE, mono, **16 kHz**, streamed continuously.
* **Server → client:** PCM signed 16-bit LE, mono, **24 kHz**.
* Binary WebSocket frames are raw audio; text frames are JSON control/transcript events.

Full event contract (including protocol v2 fields): see `backend/docs/websocket_protocol.md`.

---

# 5. Where things live

```text
backend/
  app/main.py                  FastAPI WS bridge, turn tracking, structured logs,
                               session-resumption handle store (protocol v2)
  docs/websocket_protocol.md   wire protocol v2 (authoritative)
  requirements.txt             pinned deps (google-genai==2.19.0)
mobile/
  lib/services/                controller, WS client, audio capture/playback
  android/                     Gradle 9.3.1 / AGP 9.1.0 / Kotlin 2.4.0
product_phases.md              phase plan + live progress (source of truth)
doc.md                         this file
```

Signing keys (`upload-keystore.jks`, `key.properties`) are **gitignored — repo is public**. Deploy needs only `gcloud` auth; builds happen in Cloud Build (no local Docker).

---

# 6. How it got here (compressed history)

* **Phase 0 (done ~16 Aug)** — Python diagnostic client (`test_continuous.py`) proved the full loop: mic → WebSocket → Cloud Run → Gemini Live → back → speaker. Established structured event logging and `turn_summary` records. Proved Gemini detects interruptions server-side.
* **Phase 1 (done 21 Aug)** — real Flutter Android client: state machine (IDLE→CONNECTING→LISTENING⇄RESPONDING/PLAYING/INTERRUPTING), per-turn transcript aggregation, barge-in flush + on-device measurement, network-blip auto-reconnect with backoff + keepalive stall watchdog, signed release APK, modern build toolchain. Accepted on device.
* **Protocol v2 bonus (21 Aug, pulled forward from Phase 2)** — session resumption: stable `client_session_id` from the client, server-side handle store (2 h TTL), same-Gemini-session resume on reconnect, `resumed` flag surfaced in UI. Verified end-to-end: fact spoken → socket hard-dropped → reconnect → model still knew the fact. In production.

---

# 7. Hard-won lessons (do not re-learn these)

1. **Gemini's barge-in detection works.** Early confusion ("assistant doesn't stop") was never a Gemini problem — logs showed `interrupted` firing correctly. The audible lag was **local playback buffering**: queued PCM keeps playing after generation stops. Playback flush on `interrupted` is a *client* responsibility and is now implemented.
2. **Model choice gates features.** Session resumption requires a model that actually emits *resumable* handles. `gemini-2.5-flash-native-audio-preview-12-2025` never does; `gemini-3.1-flash-live-preview` does (right after the first turn completes). Probe before assuming a capability exists.
3. **Native-audio models can't be language-locked via `language_code`.** Output language is pinned with a system_instruction ("ALWAYS respond in English") — deployed and user-verified.
4. **Keep the audio engine warm across sessions.** `flutter_pcm_sound` v3 gates playback on an internal flag that `release()+setup()` doesn't reset — dispose kills the next session's audio. Flush, don't dispose (details in the sirious-build skill notes).
5. **Plugin quirks live outside the repo.** `flutter_pcm_sound` ships `compileSdkVersion 33`; the pub-cache copy must be patched to 37 on every fresh machine. Same class of trap: AGP 9 wants an SDK platform dir literally named `android-37`, which Google no longer publishes (only `android-37.0`) — copy + fix ApiLevel.
6. **Measure on device, not in logs.** Perceived latency ≠ generation latency. The client timestamps T0 mic-capture → T1 first user transcript → T2 first assistant audio → T3 played, and shows the breakdown on screen.
7. **Don't rewrite `main.py` or the protocol casually.** They're the stable center everything else hangs off. Change only for protocol bugs or new phase requirements — and update `websocket_protocol.md` in the same commit.

---

# 8. Current status snapshot

(For the detailed, maintained checklist see `product_phases.md`.)

```text
Phase 0  voice pipe proven                       ✅
Phase 1  Flutter mobile client                   ✅ closed 21 Aug 2026
         ├ network-blip resilience               ✅
         ├ barge-in flush + measurement          ✅ (better on 3.1-flash-live)
         ├ signed release APK                    ✅
         └ smooth-voice acceptance               ✅ user-accepted 21 Aug
Session resumption (protocol v2)                 ✅ implemented, deployed, verified
Persistent transcript storage                    ❌ Phase 2 (turn_summary data already logged)
History/search UI                                ❌ Phase 2
Transcript-replay fallback (resume expired)      ❌ Phase 2 (deferred)
Auth                                             ❌ Phase 2 dependency
Long-term memory                                 ❌ Phase 3
Tools / actions                                  ❌ Phase 4+
```

---

# 9. Roadmap ahead

Phased plan with scope-in/scope-out lives in `product_phases.md`. Shape of the road:

* **Phase 2 — Session history & review:** persist `turn_summary` streams (the data is already being generated every turn), async writes that never block the live socket, storage choice (Firestore leans favorite for this scale), in-app history + transcript views, replay-fallback for expired resume handles, lightweight auth.
* **Phase 3 — Contextual memory:** extraction pipeline over transcripts → episodic/semantic/entity/task memories. Never store raw transcripts as memory.
* **Phase 4+ — Tools, calendar/email/files, then agentic behavior.** The voice interface becomes the front door to an agent, not the agent itself.

Layering principle (unchanged since the start): transport → conversation management → client UX → persistence → memory → tools → autonomy. Build in order; don't merge layers prematurely.

---

# 10. One-line status

> **Sirious is a working, interruptible, blip-resilient voice assistant on Android with session continuity across reconnects in production — Phase 1 closed 21 Aug 2026; next: Phase 2 transcript persistence and history.**
