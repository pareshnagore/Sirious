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
                               session-resumption handle store (protocol v2),
                               memory injection at connect + extraction kick at end
  app/store.py                 Firestore session history (async queue+writer)
  app/memory.py                Phase 3 memory: extraction, embeddings, dedup,
                               retrieval, /memories backing (same queue+writer pattern)
  app/tools.py                 Phase 4 tool registry: declarations, handlers,
                               gating (SIRIOUS_TOOLS/TAVILY_API_KEY/
                               SIRIOUS_REMINDERS), dispatch, per-call audit,
                               web_search + add_note + reminders (draft/
                               confirm/cancel, server-side NL time resolve,
                               Cloud Tasks scheduling)
  app/fcm.py                   FCM push: device_tokens registry (self-pruning),
                               raw HTTP v1 send via ADC, reminder fanout
  docs/websocket_protocol.md   wire protocol v2 (authoritative)
  recall_test.py               Phase 3 north-star recall harness (edge-tts E2E)
  requirements.txt             pinned deps (google-genai==2.19.0, google-cloud-tasks,
                               pyjwt, tzdata)
mobile/
  lib/services/                controller, WS client, audio capture/playback,
                               history_api, memory_api, push_service (FCM)
  lib/ui/                      voice session, history, transcript detail, memories
  android/                     Gradle 9.3.1 / AGP 9.1.0 / Kotlin 2.4.0,
                               google-services plugin + google-services.json
product_phases.md              phase plan + live progress (source of truth)
doc.md                         this file
```

Signing keys (`upload-keystore.jks`, `key.properties`) are **gitignored — repo is public**. Deploy needs only `gcloud` auth; builds happen in Cloud Build (no local Docker).

---

# 6. How it got here (compressed history)

* **Phase 0 (done ~16 Aug)** — Python diagnostic client (`test_continuous.py`) proved the full loop: mic → WebSocket → Cloud Run → Gemini Live → back → speaker. Established structured event logging and `turn_summary` records. Proved Gemini detects interruptions server-side.
* **Phase 1 (done 21 Aug)** — real Flutter Android client: state machine (IDLE→CONNECTING→LISTENING⇄RESPONDING/PLAYING/INTERRUPTING), per-turn transcript aggregation, barge-in flush + on-device measurement, network-blip auto-reconnect with backoff + keepalive stall watchdog, signed release APK, modern build toolchain. Accepted on device.
* **Protocol v2 bonus (21 Aug, pulled forward from Phase 2)** — session resumption: stable `client_session_id` from the client, server-side handle store (2 h TTL), same-Gemini-session resume on reconnect, `resumed` flag surfaced in UI. Verified end-to-end: fact spoken → socket hard-dropped → reconnect → model still knew the fact. In production.
* **Phase 2 (done 22 Aug)** — persistent session history: Firestore (native, asia-south1) with one doc per logical conversation (`client_session_id`; resuming reconnects extend the same doc), async queue+writer with turn-level writes and disconnect flush that can never block or break the voice path; REST history API; bearer-token auth on REST + WS handshake; mobile History/Transcript screens with token in `flutter_secure_storage`; transcript-replay fallback (recent turns injected into `system_instruction` when no live resume handle exists). E2E verified on device: spoken question → Gemini → Firestore → REST → phone screen; replay fallback verified live across sessions.
* **Phase 3 (done 22 Aug)** — contextual memory: after each session ends the backend extracts structured memories (episodic / semantic / entity / task) with one flash-model call over a handler-provided turn snapshot (no read-after-write races), embeds them (`gemini-embedding-001`), dedups by cosine ≥ 0.90 into provenance-growing merges, and stores them in the Firestore `memories` collection with session/turn provenance and a turn-ID watermark per conversation. At every new connect a bounded block (top facts + date-stamped episodic index) is injected into `system_instruction`. `GET /memories?q=` gives conversational semantic search; `DELETE /memories/{id}` soft-deletes. Mobile Memories screen (view/search/delete/tap-through). North-star recall test PASSED in prod: peacock-color session N recalled as "yes, we discussed peacocks" from session N+1 on a different session id.
* **Agentic recall + deletion (22 Aug, same day)** — Paresh chose full agentic recall: `search_past_conversations` function-calling tool on the Live session; the model decides when to search, gets top-5 hits WITH cosine scores and judges relevance itself (kangaroo negative-probe answered honestly). Session deletion (`DELETE /sessions/{id}`) cascades: provenance stripped, sourceless memories removed, watermark cleared; swipe-to-delete in the History UI. Release APK deployed to device; Paresh accepted Phase 3 on-device.

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
Persistent transcript storage                    ✅ Phase 2 done 22 Aug 2026 (Firestore)
History/search UI                                ✅ Phase 2 (list + transcript detail on device)
Transcript-replay fallback (resume expired)      ✅ Phase 2 (live-verified 22 Aug)
Auth                                             ✅ Phase 2 (bearer token, REST + WS)
Long-term memory                                 ✅ Phase 3 done 22 Aug 2026 (recall PASS, user-accepted)
Tools / actions                                  ✅ Phase 4 COMPLETE 24 Aug 2026
                                                 (registry, web_search via Tavily,
                                                 add_note, audit log, reminders
                                                 end-to-end: voice draft→confirm→
                                                 Cloud Tasks→FCM push — verified
                                                 on device)
```

---

# 9. Roadmap ahead

Phased plan with scope-in/scope-out lives in `product_phases.md`. Shape of the road:

* **Phase 3 — Contextual memory:** extraction pipeline over transcripts → episodic/semantic/entity/task memories. Never store raw transcripts as memory. The Phase 2 Firestore store is the raw material.
* **Phase 4 (COMPLETE 24 Aug 2026)** — tools & actions: server-side tool registry (`app/tools.py`); `web_search` (Tavily, provider-swappable) and `add_note` (Firestore) behind one generic dispatcher with a per-call audit log (`tool_audit`); confirmation scaffold for future destructive tools; **reminders end-to-end** — `create_reminder` takes the user's own words ("friday morning"), resolved server-side in Asia/Kolkata (no timestamps in the prompt → stable prefix, no stale clocks); spoken confirm → `confirm_reminder` schedules a one-shot Cloud Tasks HTTPS task (deterministic names); fire lands on `/internal/fire-reminder` (OIDC-verified, idempotent via Firestore transaction) → FCM push to registered devices (`device_tokens`, self-pruning) → tray notification on the phone, on-device verified. Temporal grounding via `get_current_time` tool.
* **Phase 5+ — ambient/multi-speaker, people recognition, deeper agentic behavior.** The voice interface becomes the front door to an agent, not the agent itself.

Layering principle (unchanged since the start): transport → conversation management → client UX → persistence → memory → tools → autonomy. Build in order; don't merge layers prematurely.

---

# 10. One-line status

> **Sirious is a working, interruptible, blip-resilient voice assistant on Android with session continuity, persistent history, contextual memory with agentic recall, and real tools in production — web search, notes, and end-to-end reminders (voice → Cloud Tasks → FCM push to the phone, on-device verified). Phase 4 complete 24 Aug 2026.**
