# Sirious — Product Phases

**Date:** 16 August 2026  
**Purpose:** Map the long-term Sirious vision to incremental, shippable phases. Use this to avoid scope creep and to decide what belongs in the current milestone vs later work.

---

## North star

Sirious is a **persistent personal assistant** that:

- Works primarily on **mobile**, also on laptop
- Listens during conversations and meetings when enabled
- Stays **silent unless asked** (or explicitly invoked)
- Remembers **people, context, and conversations** over time
- Eventually supports **vision** and **actions** (tasks, calendar, search, etc.)

This document breaks that vision into phases. Each phase should be **usable on its own** before starting the next.

---

## How phases relate to engineering layers

Engineering layers (from `doc.md`):

```text
Layer 1  Real-time voice transport
Layer 2  Conversation / turn management
Layer 3  Client UX
Layer 4  Persistent conversation
Layer 5  Memory
Layer 6  Tools
Layer 7  Autonomous agent behavior
```

Product phases span multiple layers. A single product phase may complete parts of several layers.

| Product phase | Primary engineering layers |
|---------------|----------------------------|
| Phase 0       | Layer 1–2 (server)         |
| Phase 1       | Layer 1–3 (client)         |
| Phase 2       | Layer 3–4                  |
| Phase 3       | Layer 4–5                  |
| Phase 4       | Layer 5–6                  |
| Phase 5       | Layer 6–7                  |
| Phase 6       | Cross-cutting (new modalities) |

---

**Current status**

**Phase 3 (contextual memory) is complete — deployed, recall-test PASS in prod, user-accepted on device (22 Aug 2026).**

```text
✅ Cloud Run WebSocket bridge
✅ Gemini Live native audio loop
✅ Streaming mic in / audio out
✅ Input & output transcription
✅ Turn detection & Gemini barge-in detection
✅ Structured server logging & turn summaries
✅ Session resumption (protocol v2, automated)
✅ Flutter mobile client (Phase 1 closed 21 Aug 2026)
✅ Firestore persistence: turn-level writes, async writer, flush on disconnect
✅ Bearer-token auth on REST + WS handshake (SIRIOUS_AUTH_TOKEN)
✅ History REST API (GET /sessions, GET /sessions/{id}) + mobile history UI
✅ On-device E2E verified: voice → Gemini → Firestore → REST → phone screen
✅ Transcript-replay fallback for expired resume handles (live-verified)
✅ Contextual memory (Phase 3, 22 Aug 2026): extraction pipeline, embeddings,
   dedup, wallet injection, agentic search tool, Memories UI, session
   deletion with memory cascade — recall test PASS in prod
✅ Tools & actions v1 (Phase 4, 23 Aug 2026): server-side tool registry,
   web_search (Tavily) + add_note (Firestore), per-call audit log —
   both tools live-proven in prod (structured logs + spoken answers)
✅ Reminders end-to-end (Phase 4 closed 24 Aug): voice draft→confirm→
   Cloud Tasks→OIDC fire→FCM→tray notification+badge, on-device verified
❌ Multi-speaker (Phase 5 — started 25 Aug 2026), vision
```

**Active focus:** **Phase 5 IN PROGRESS (started 25 Aug 2026)** — ambient & multi-speaker
mode. Architecture agreed: side-STT ambient (NOT always-on Gemini), ducking for answer
time, Google STT v2 (Marathi-capable streaming) as launch provider behind a provider
interface. Working plan + fork alternatives in the Phase 5 section below; echo/barge-in
research in `docs/echo_bargein_research.md`.

### Phase 1 progress (17 Aug 2026)

```text
✅ Flutter Android client builds (debug APK) & installs on physical device (SM E346B, Android 16)
✅ WebSocket v1 client (binary audio + JSON events) matching backend/docs/websocket_protocol.md
✅ Mic capture (16 kHz PCM) → send; receive → playback queue → speaker; event handler
✅ Client state machine IDLE→CONNECTING→LISTENING⇄RESPONDING/PLAYING
✅ Playback cancellation on `interrupted` (clear queue + flush)
✅ Transcript fragments aggregated per turn (not word-by-word)
✅ Basic session UX: connect, listening indicator, end session
✅ Latency timestamps (T0–T3) shown in UI
✅ FIXED: no audio on follow-up sessions (flutter_pcm_sound _needsStart not reset by release+setup;
  keep engine warm across sessions, feed via direct drain)
✅ FIXED: transcript text was persisting across sessions (now cleared at session start)
✅ FIXED (19 Aug): app survives brief network blips — auto-reconnects to a fresh
  server/Gemini session (client-side, exponential backoff 1s→8s, 5 attempts) and
  resumes Listening; mid-utterance partial turn committed so nothing is lost;
  keepalive ping + stall watchdog catches sockets that silently stop; verified live
  (airplane-mode toggle: Listening → Reconnecting → Listening, transcript preserved)
✅ Barge-in latency measured on device (target ~200–500 ms) — user-verified 21 Aug:
   interruption behavior BETTER on gemini-3.1-flash-live than the 2.5 native-audio model
✅ Full "smooth voice experience" acceptance on device — user-accepted 21 Aug
✅ Release APK (signed) — built & verified on 20 Aug 2026 (see "Android build/keystore" below)
✅ Upstream protocol/docs re-verification — done 21 Aug (official Live session-management
   docs cross-checked; websocket_protocol.md updated to v2: session resumption)
✅ BONUS (Phase 2 item pulled forward): session resumption across network blips —
   implemented + user-verified on device 21 Aug; prod runs gemini-3.1-flash-live
   (SIRIOUS_MODEL env var) since 2.5-native-audio never emits resumable handles
```

### Android build toolchain + keystore (20 Aug 2026)

> **For other agents/PCs:** before any `flutter build` on a fresh machine, read this. The
> project **cannot** be built with the default toolchain of an old Flutter — it needs the
> versions below. If a build fails, check these first.

- **Flutter SDK ≥ 3.47** (Dart ≥ 3.11). Older Flutter (e.g. 3.35) fails `pub get` because
  `path_provider` needs Dart ≥ 3.10.
- **Gradle 9.3.1**, **AGP 9.1.0**, **Kotlin 2.4.0** — pinned in
  `mobile/android/gradle/wrapper/gradle-wrapper.properties` and
  `mobile/android/settings.gradle.kts`. These are Flutter 3.47's *tested defaults* —
  don't downgrade them. They're needed because Flutter uses Android Studio's bundled JBR
  (Java 25), and old Gradle (8.14) cannot run on Java 25.
- **No JDK install needed** — Java 25 comes from Android Studio's bundled JBR
  (`/Applications/Android Studio.app/Contents/jbr`). Gradle 9.3.1 supports it.
- **Android build-tools 37.0.0** must be installed (`sdkmanager "build-tools;37.0.0"`)
  — the build needs `platforms/android-37` + build-tools 37 for `compileSdk = 37`.

**`flutter_pcm_sound` compileSdk patch (IMPORTANT, easy to miss):**
- `flutter_pcm_sound` 3.3.3 (latest) ships `compileSdkVersion 33`, but its transitive
  AndroidX deps require android-34+. Build fails with
  `:flutter_pcm_sound:checkDebugAarMetadata` / `CheckAarMetadataWorkAction`.
- **Fix:** edit `~/.pub-cache/hosted/pub.dev/flutter_pcm_sound-3.3.3/android/build.gradle`
  → `compileSdkVersion 37`. ⚠️ This lives in the pub cache, **not** the repo — a fresh
  `flutter pub get` reverts it. Vendor the plugin or ask the maintainer before a real
  distribution.

**Signing / keystore (do NOT commit to this repo — it is PUBLIC):**
- Release keystore: `mobile/android/app/upload-keystore.jks`
  (alias `sirious`). Passwords: `mobile/android/key.properties`.
- **Both are gitignored** and must stay out of the public GitHub repo (see below).
- `mobile/android/app/build.gradle.kts` loads them, falls back to debug signing
  if `key.properties` is absent (so a fresh clone still builds).
- **Back these up off-machine** — losing the keystore means you can never update the app
  under `com.sirious.sirious`.

**Remote push from this machine (important):**
- The local agent (Claude Code) does **not** push to GitHub — the user has a
  **fine-grained Personal Access Token** for `git push` and their credentials are not
  available to the agent. Agents commit locally; **the user pushes** (or a token-based
  `git push` via `gh`/the user).
- The GitHub repo `pareshnagore/Sirious` is **public**.

---

## Phase 0 — Prove the voice pipe (DONE)

### Goal

Validate that real-time voice conversation works end-to-end through Cloud Run and Gemini Live.

### User experience

Developer runs a test client on laptop; speaks; hears Gemini respond. No product UI.

### Scope

**In:**

- FastAPI WebSocket endpoint (`/ws`)
- Gemini Live session (native audio model)
- 16 kHz PCM in, 24 kHz PCM out
- JSON control/transcript events
- Turn tracking and structured logs
- Barge-in detection on server (Gemini `interrupted` event)

**Out:**

- Mobile app
- Polished interruption playback on test client
- Persistent transcripts
- Auth, billing, multi-user

### Success criteria

- [x] Stable voice loop for several minutes
- [x] Streaming transcripts received
- [x] Gemini detects user interruption (server logs confirm)
- [x] Deployed to Cloud Run (`asia-south1`)

### Notes

- Python `test_continuous.py` is a **diagnostic client**, not the product.
- Local playback buffering can make barge-in *feel* broken even when Gemini behaves correctly. The real client must flush audio on `interrupted`.

---

## Phase 1 — Personal voice assistant on mobile (DONE)

### Goal

A real **Flutter Android client** that delivers a reliable 1:1 voice conversation with Sirious — the first experience worth using daily.

### User experience

```text
┌──────────────────────────────┐
│          Sirious             │
│                              │
│  You: Tell me about India.   │
│                              │
│  Sirious: India is...        │
│                              │
│          ● Listening         │
│     [ End session ]          │
└──────────────────────────────┘
```

User opens app → taps to start session → speaks naturally → hears response → can interrupt mid-response → session ends cleanly.

### Scope

**In:**

- Flutter Android app (iOS later if needed)
- WebSocket client matching frozen server protocol
- Decoupled architecture:
  - Mic capture → WS send
  - WS receive → playback queue → speaker
  - WS receive → event handler (transcripts, lifecycle)
- Client state machine: `IDLE → CONNECTING → LISTENING ⇄ RESPONDING/PLAYING`
- **Playback cancellation** on `interrupted` (clear queue, flush audio output)
- Aggregate transcript fragments for display (one line per turn, not word-by-word spam)
- Basic session UX: connect, listening indicator, end session
- Client-side latency timestamps (T0–T3)
- Align `backend/docs/websocket_protocol.md` with actual `main.py` behavior

**Out:**

- Multi-speaker / table mode
- Long-term memory across sessions
- Tools (calendar, email, etc.)
- User accounts (can use single shared deployment initially)
- Session resumption automation (defer unless blocking testing)
- Fancy UI, widgets, themes

### Success criteria

- [ ] Voice conversation works on a physical Android device
- [ ] Barge-in feels immediate (user hears assistant stop within ~200–500 ms of interrupting)
- [ ] App survives brief network blips without crashing
- [ ] Transcripts display coherently per turn
- [ ] End-to-end latency measured and logged on device

### Dependencies

- Phase 0 server stable and protocol documented

### Risks

- Android audio permissions, background recording, Bluetooth/ speakerphone echo
- WebSocket + binary frame handling in Flutter
- Cloud Run cold start adding first-connection delay

---

## Phase 2 — Session history & review (DONE 22 Aug 2026)

### Goal

Conversations are **saved and searchable**. User can revisit what was said and brainstorm alone later.

### User experience

- ✅ After a session, user sees a transcript timeline — **delivered** (History list → transcript detail, verified on device)
- ➡️ ~~Can search past sessions ("what did we discuss about Mumbai?")~~ — **moved to Phase 3**: a raw substring search would be a stopgap that memory-based retrieval immediately supersedes; building it once, on top of extracted memory
- 🟡 Can start a new session with optional reference to past context — **partially delivered, explicitly to Phase 3**: the replay fallback already injects past turns into Gemini, but only for reconnects of the *same* session id (invisible to the user). The deliberate "ask about / continue a past conversation" UX is Phase 3's retrieval-at-session-start scope

> Phase 2 is complete against its committed scope ("In:" list + success criteria below). The two UX bullets above were aspirational vision text; they are tracked as Phase 3 work now, not silently dropped.

### Progress (22 Aug 2026)

```text
✅ Firestore (native, asia-south1) persistence — one doc per logical
   conversation (doc id = client_session_id; resuming reconnect extends
   the same doc), async queue+writer, turn-level writes, disconnect flush;
   writer failures can never take down the voice path
✅ REST API: GET /sessions (list) + GET /sessions/{id} (full transcript)
✅ Bearer-token auth on REST + WS handshake (rejected before accept());
   token lives in flutter_secure_storage on device
✅ Mobile: History list + Transcript detail screens, key-icon token entry,
   WS client sends ?token= — verified on device with live prod data
✅ E2E proven in prod: spoken question → Gemini answer → Firestore →
   REST → phone screen ("The capital of Japan is Tokyo.")
✅ Transcript-replay fallback: no live resume handle → recent turns from
   Firestore injected into system_instruction at connect; live-verified
   (fact → clean stop → fresh session still knew the fact)
```

**Out:**

- ~~Server-side session resumption on `go_away`~~ → **DONE (21 Aug 2026, protocol v2)** —
  client stable `client_session_id` + backend handle store/resume; verified live:
  spoke a fact → hard socket drop → reconnected (`resumed=true`) → Gemini still knew the fact.
- ~~Transcript-replay fallback for expired handles~~ → **DONE 22 Aug 2026** (see above).

**Out (still):**

- Semantic memory extraction
- Speaker attribution
- Tool execution
- Audio recording archival (text-first; optional audio later)

### Success criteria

- [x] Every completed turn persisted within seconds of `turn_complete`
- [x] User can open a past session and read full transcript
- [x] Sessions longer than 8 minutes work via server-side resumption
- [x] No data loss on normal disconnect

### Dependencies

- Phase 1 client working
- Auth (even lightweight) before storing personal data at scale

---

## Phase 3 — Contextual memory (DONE 22 Aug 2026 — deployed, recall-test PASS, user-accepted on device)

### Goal

Sirious **remembers facts about the user, projects, and people** across sessions — not just raw transcripts.

### User experience

```text
Week 1: "I have an interview with Acme next Tuesday."
Week 2: "What should I prepare for my interview?"
Sirious:  "For your Acme interview on Tuesday, you might..."
```

**North-star recall test (Paresh's objective, agreed 22 Aug 2026):**

```text
Session N:   "What is the color of a peacock?"        (casual conversation)
Session N+1: "Did I have any conversation about birds?"
Sirious:     "Yes — you talked about peacocks and asked their color."
```

The assistant must recall **the act of talking about something** ("did we
discuss X?"), not just answer questions from facts. Single user first; the
same memory must later attach to *who said what* so multi-speaker sessions
("someone said something about…") keep that context. This drives the design:
memories need provenance (session/turn/speaker refs), and retrieval must match
topics, not just keywords.

### Scope

**In:**

- Memory pipeline:
  ```text
  turn summaries → episodic store → extraction job → structured memory
  ```
- Memory types:
  - **Episodic:** session/turn references (provenance — "you discussed this in session on 22 Aug")
  - **Semantic facts:** preferences, dates, project details
  - **Entities:** people, companies, projects, topics (names only at first) — topics power the "birds → peacock" recall
  - **Tasks:** action items mentioned in conversation
- Retrieval at session start (inject relevant memory into Gemini system context)
- **Conversational search over past sessions** (moved here from Phase 2):
  "what did we discuss about Mumbai?" answered from extracted memory +
  transcript references — not raw substring grep, which Phase 3 supersedes
- **Explicit past-context UX** (moved here from Phase 2): start/reference a
  past session deliberately; generalizes the Phase 2 replay fallback beyond
  same-session reconnects
- User controls: view memory, delete incorrect entries
- "Silent assistant" behavior tuning via system instructions

**Out:**

- Voice-based speaker identification
- Automatic task execution
- Vision

### Success criteria

- [x] Facts mentioned in session N are usable in session N+1 without user re-stating them
- [x] **Recall test:** "Did I have any conversation about birds?" after an earlier peacock-color session → "Yes — you talked about peacocks and asked their color" (topic-level episodic recall with provenance) — **PASS live in prod** (harness + manual on-device)
- [x] User can see and delete stored memories — Memories screen (view/search/delete) verified by Paresh on device
- [x] Memory injection does not blow context window or add unacceptable latency (bounded block, injected once per connect)

### Progress (22 Aug 2026 — M1–M4 built locally, tests green; deploy + on-device E2E pending user go-ahead)

```text
✅ Backend app/memory.py: MemoryStore mirroring store.py's pattern — async
   queue + single writer task, hot path never blocks or breaks the voice
   loop; SIRIOUS_MEMORY=1 gate + NullMemoryStore for local dev
✅ Extraction: one flash-model call per session end (structured JSON:
   type/text/topics/entities/turn_refs), turn-ID watermarked per doc so a
   resumed conversation re-extracts only its tail; failures leave the
   watermark untouched and retry at next extraction
✅ Embeddings: gemini-embedding-001 (RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY,
   768-dim) stored on each memory doc
✅ Dedup: cosine ≥ 0.90 → append provenance + bump times_seen instead of
   inserting a duplicate memory
✅ Retrieval: exact in-process cosine ranking over active memories (no
   Firestore vector index needed at personal scale)
✅ Injection: bounded memory block (top facts/tasks + date-stamped recent
   episodic index) into system_instruction at every WS connect
✅ REST: GET /memories (list or ?q= semantic search), DELETE /memories/{id}
   (soft delete); bearer auth inherited
✅ Mobile: MemoriesScreen (view / semantic search / delete with confirm /
   tap-through to source transcript), MemoryApi, psychology-icon entry on
   voice screen; flutter analyze clean, widget tests pass
✅ Recall-test harness backend/recall_test.py: edge-tts sessions N ("peacock
   color") and N+1 ("did I talk about birds?") on DIFFERENT client_session_ids
   (so replay fallback can't explain a pass), polls /memories until session
   N's provenance appears, asserts "peacock" in the answer
✅ Tests: 36 pass (15 new Phase 3: extraction/watermark/dedup/ranking/
   injection-bounds/null-mode/REST incl. 503 paths/provenance cascade)
❌ ~~Deploy to Cloud Run~~ → **DONE 22 Aug** (revisions 00023–00027;
   SIRIOUS_MEMORY=1 + --no-cpu-throttling)
❌ ~~On-device E2E recall test + Memories UI against prod~~ → **DONE 22 Aug**
   (recall harness PASS; Paresh verified Memories screen, search, deletion
   and live voice recall on device)
✅ Agentic recall (Paresh's call — Option B): `search_past_conversations`
   function-calling tool on the Live session; model decides when to search,
   server embeds query → cosine over all memories → top-5 hits WITH scores
   (the model judges relevance itself). Live-proven both directions:
   birds→peacock PASS; kangaroo probe answered honestly "no kangaroo
   conversations… we've discussed birds and peacocks"
✅ Session deletion with memory cascade (P3 add-on): DELETE /sessions/{id}
   hard-deletes the doc, strips its provenance entries from memories,
   removes sourceless memories, drops the extraction watermark. Mobile:
   swipe-to-delete on History with confirm dialog. Live-verified.
```

#### Deployment notes (learned the hard way)

- `gemini-2.5-flash` (extraction default) was RETIRED by Google — prod logs
  404'd on first extraction. Now `gemini-3.6-flash` via SIRIOUS_EXTRACT_MODEL.
- `FunctionResponse` MUST echo `fc.id` or Google AI 400s the whole Live
  session — symptom: silence (no audio out, no turn_complete), only visible
  in structured logs as `gemini_error`.

#### Design decisions worth remembering

- **Extraction reads a handler-provided turn snapshot, not Firestore.**
  `store.snapshot_turns()` is read synchronously at WS teardown and passed to
  the memory queue — zero read-after-write races with the Phase 2 writer.
- **Turn-ID watermark (`memory_meta/{doc}` → extracted_turn_ids), not a
  count:** a resumed handler's snapshot contains ALL turns of the
  conversation; a count would wrongly skip the head.
- **Dedup merges instead of skipping:** repeated mentions grow provenance
  (multi-session evidence), which later powers "you've mentioned this N times".
- **Speaker field exists but is null** — single-user first; multi-speaker
  attribution (Phase 5+) extends provenance without a schema break.
- **Episodic memories are deliberately over-collected** (even trivia): the
  product must answer "did we ever talk about X?" for anything discussed.
  Broad topic categories (birds ← peacock) power the north-star recall.

### Design principle

**Do not store every transcript line as memory.** Extract selectively. Raw transcript (Phase 2) and durable memory (Phase 3) are separate stores.

---

## Phase 4 — Tools & actions (IN PROGRESS — v1 deployed 23 Aug 2026)

### Goal

Sirious can **do things**, not only talk — calendar, reminders, web search, notes, team task assignment.

### User experience

```text
User: "Remind me to follow up with Raj on Friday."
Sirious: "Done. I'll remind you Friday morning."

User: "Search for the latest news on India's high-speed rail."
Sirious: [ searches, summarizes verbally ]
```

### Scope

**In:**

- Tool registry on server (not in Gemini Live hot path initially)
- Orchestration layer: voice → intent → tool call → spoken result
- Initial tools (pick 2–3):
  - Web search
  - Reminders / calendar (Google Calendar API)
  - Notes capture
- Confirmation for destructive or external actions
- Audit log of tool invocations

**Out:**

- Fully autonomous agent (acts without user awareness)
- Complex multi-step workflows
- Finance, email send (higher trust bar)

### Success criteria

- [ ] At least two tools work reliably via voice ← **web_search + add_note pass in prod probes; pending Paresh's on-device voice verification**
- [ ] User is informed before irreversible actions ← scaffolded (`requires_confirmation` flag); no shipped tool needs it yet
- [ ] Tool failures degrade gracefully (spoken error, no crash) ← verified: handler errors become structured payloads, model speaks a graceful fallback

### Progress (23 Aug 2026 — v1 built, deployed rev 00030, live-proven)

```text
✅ backend/app/tools.py: per-connection ToolRegistry — declarations,
   handlers, gating, dispatch; every call audited exactly once.
   Gates: SIRIOUS_TOOLS=1 (master), TAVILY_API_KEY (web_search),
   SIRIOUS_PERSIST=1 (add_note). search_past_conversations keeps its
   Phase 3 gate (memory enabled) — Phase 3 behavior byte-for-byte.
✅ main.py: hardcoded tool branch replaced by ONE generic dispatcher;
   registry drives LiveConnectConfig.tools; tool-usage hints appended
   to system instruction only for tools actually registered.
✅ web_search: TavilyProvider behind a provider interface (swap Serper/
   Brave = one adapter class); top-5 title/snippet/url, snippets capped
   at 350 chars; empty query / no results / provider failure all return
   structured payloads the model can speak gracefully.
✅ add_note: Firestore tool_notes/{id} (text ≤4000 chars, optional topic,
   session_ref provenance, created_at).
✅ Audit log: EVERY invocation (ok/error/unknown_tool) → tool_audit doc,
   fire-and-forget so audit can never break the voice path; args
   truncated to 500 chars in the log.
✅ Confirmation scaffold: ToolSpec.requires_confirmation exists; first
   destructive tool implements draft→confirm→execute against it.
✅ Tests: 58 pass (22 new: registry gating, wire shape, dispatch+audit,
   Tavily request mapping, notes store, audit Firestore mode, P3 parity)
✅ Prod probes (rev 00030): web_search answered current bullet-train
   news from live results (tool_called→tool_result ok, 2.5 s);
   add_note saved "filter coffee place in Indiranagar" with model-chosen
   topic "coffee plans" (outcome ok) — both visible in structured logs
❌ Paresh's on-device voice verification of both tools
❌ Reminders — direction AGREED 23 Aug: FCM push + Cloud Tasks one-shot
   scheduling (no polling). Deferred out of v1 deliberately.
```

#### Reminders — chunk 1 built locally (23 Aug 2026, NOT yet deployed)

```text
✅ tools.py: ReminderStore (Firestore `reminders/{id}`: text ≤500 chars,
   due_ts/due_at UTC, topic, status, session_ref) + InMemoryReminderStore
   null-mode double. Tools behind SIRIOUS_REMINDERS=1 gate (stacks on
   SIRIOUS_TOOLS; Firestore store requires SIRIOUS_PERSIST):
   - get_current_time: server clock in SIRIOUS_TZ (default Asia/Kolkata).
   - create_reminder(text, due_at): writes status="draft", returns
     draft_id; NOTHING scheduled yet. Validation ≥2 min future, ≤365 days.
     due_at takes the USER'S OWN WORDS ("friday morning", "in 20 minutes")
     resolved SERVER-SIDE by a closed-grammar resolver — in N
     min/hours/days/weeks · today/tonight/tomorrow/day after tomorrow ·
     [next] <weekday> · H[:MM] [am|pm] · period defaults (morning 9:00,
     evening 19:00, night 21:00) · ISO 8601 still accepted; naive stamps
     read as user-local wall clock. Spoken draft-back rendered in user tz.
   - confirm_reminder(reminder_id): draft→scheduled. Idempotent on
     re-confirm; stale drafts (>15 min DRAFT_TTL_S) fail closed. This IS
     the user-consent step (requires_confirmation stays unused).
   - cancel_reminder(reminder_id): cancelled (fired/cancelled are no-ops).
✅ main.py: NO date/time in system_instruction (a per-session timestamp
   would churn the prompt prefix on every reconnect and go stale on
   long/resumed sessions — Paresh's catch). Temporal grounding lives in
   get_current_time instead; prompt prefix stays byte-stable.
✅ requirements.txt: +tzdata (Windows zoneinfo needs it; no-op on Linux).
✅ Tests: 95 pass (37 new since v1: parse matrix incl. min-lead boundary,
   NL resolver anchored at fixed NOW = Fri 15 Jan 2027 10:00 IST, lifecycle,
   idempotent confirm, stale-draft fail-closed, Firestore roundtrip via
   FakeDb, gating, store-selection by config, audited dispatch flow, and
   a regression guard asserting system_instruction carries NO timestamp).
⚠️ By design in chunk 1: confirm flips status only — no Cloud Tasks task
   is scheduled yet, nothing can fire. Chunks 2–4: Cloud Tasks + fire
   endpoint; Firebase Admin push + token registration; Android FCM client.
```

#### Reminders — chunk 2 built + DEPLOYED + prod-verified (23 Aug 2026)

```text
✅ tools.py: NullScheduler (SIRIOUS_TASKS_* unset → consent-only) vs
   CloudTasksScheduler — one-shot HTTPS task per confirmed reminder,
   DETERMINISTIC task names (rem-{id}-{due_ts}) so an ambiguous Cloud
   Tasks timeout can't double-schedule. confirm: flip→schedule→store
   task_name; schedule failure reverts status to draft (consent never
   stranded without a task). cancel: best-effort task delete first;
   cancelled-status guard catches any fire that still arrives.
✅ main.py POST /internal/fire-reminder: OIDC verify → idempotent
   scheduled→fired flip via Firestore transaction (@async_transactional
   precondition) → push hook (chunk-3 seam, currently logs only).
   Response-code policy: fired/already-fired/cancelled → 2xx (stops
   retries); early-fire beyond 5-min grace → 409 (retry later); unknown
   → 404. Bearer-auth selftest endpoint /internal/reminders/selftest.
✅ Prod probe rev 00040 PASS: selftest → task created → fired at due
   instant → single request, HTTP 200, no retries; Firestore doc
   scheduled→fired with fired_at stamped.
✅ Infra: Cloud Tasks API enabled; queue sirious-reminders@asia-south1;
   SA sirious-reminders-signer (TokenCreator on itself, run.invoker on
   sirious-api); compute SA enqueuer on project+queue. Env on Cloud Run:
   SIRIOUS_TASKS_QUEUE / SIRIOUS_FIRE_URL (= canonical run.app URL — new
   URL after rev 00031!) / SIRIOUS_FIRE_OIDC_SA.
❗Prod lessons (revs 00033–00039, each a separate fix-commit):
   - Cloud Tasks delivers OIDC in AUTHORIZATION header ("Bearer …"),
     NOT X-Goog-Iap-Jwt-Assertion (that's IAP).
   - google.auth.jwt.decode needs explicit cert iterable; use
     google.oauth2.id_token.verify_token + pyjwt for JWK-set certs.
   - Do NOT assert iss==cloud.google.com/iap on task tokens; bind via
     email == SIRIOUS_FIRE_OIDC_SA instead.
   - Async Firestore txn = @async_transactional ONLY; neither await
     db.transaction(fn) nor `async with txn:` begins the transaction.
⚠️ Remaining: chunk 3 (Firebase Admin FCM send + device_tokens
   registration), chunk 4 (Android client: google-services.json, FCM
   service, notification UI), Paresh's voice verification of reminders.
```

#### Reminders — chunks 3+4 built + DEPLOYED + prod-verified (24 Aug 2026)

```text
✅ Firebase provisioned via API (no console): addFirebase on sirious-2026,
   Android app com.sirious.sirious registered, google-services.json fetched
   + decoded into mobile/android/app/.
✅ backend/app/fcm.py (chunk 3): DeviceTokenStore (Firestore
   device_tokens/{sha256(token)} — re-register overwrites, never dups);
   send_push = raw FCM HTTP v1 via ADC OAuth2 (google.auth, NO
   firebase-admin dep), urllib inside asyncio.to_thread;
   deliver_reminder_to_all_devices fans out + prunes dead tokens.
   UNREGISTERED (404) AND 400 "not a valid FCM registration token" both
   count as dead (rev 00041 lesson).
✅ main.py: POST /devices/register + /devices/unregister (bearer-auth);
   fire endpoint passes real push_send after the idempotent flip — push
   failure can never cause a retry of an already-fired reminder.
✅ Android (chunk 4): settings/app gradle.kts += google-services 4.4.2;
   pubspec += firebase_core 4.13 / firebase_messaging 16.5; PushService
   (init before runApp fire-and-forget, POST_NOTIFICATIONS request, token
   fetch + onTokenRefresh → /devices/register with bearer token from
   secure storage, top-level background handler). analyze clean, widget
   test pass, debug APK builds. NOT installed yet — phone was away;
   when back: adb install -r (NOT flutter install — wipes secure storage).
✅ Prod probe rev 00042 PASS: register fake token → selftest → fired on
   time → FCM 400 invalid-token → device doc auto-pruned (404). Full
   schedule→fire→fanout→prune chain exercised live.
⏳ Unverifiable without phone: real FCM token registration + notification
   rendering on device. Checklist for Paresh: adb install -r new APK →
   open app once (auto-registers token, asks notification permission) →
   voice "remind me to X in 5 minutes" → confirm → expect push ~5 min.
```

#### Reminders — ON-DEVICE VERIFIED (24 Aug 2026, Paresh back) — Phase 4 COMPLETE

```text
✅ Release APK installed via adb install -r (keystore signature matched —
   secure storage/token preserved; NOTE: debug-signed build can't update
   the release install, INSTALL_FAILED_UPDATE_INCOMPATIBLE).
✅ Real FCM token registered to /devices/register → device_tokens doc live.
❗On-device lesson: PushService initially read secure-storage key
   'auth_token' but AuthService stores 'sirious_api_token' → registration
   401'd silently. Fixed, rebuilt, reinstalled (commit 0c3ac4d).
✅ Push rendered on device: FCM send → tray notification + app-icon badge
   confirmed via dumpsys NotificationRecord (pkg=com.sirious.sirious,
   FCM-Notification) + screenshot showing badge "1" on the Sirious icon.
⚠️ Known behavior (deferred polish): app FOREGROUND → FCM hands the
   message to onMessage (debugPrint only, no tray render) — by design;
   background/terminated renders fine. UI toast for foreground is later.
✅ FULL CHAIN NOW PROVEN: voice draft → confirm → Cloud Tasks → fire
   endpoint (OIDC) → FCM → phone notification.
Remaining for Phase 4: Paresh's natural voice test ("remind me to X in 5
minutes" → confirm → push). Then: notes-retrieval design (parked), Phase 5.
```

#### Deployment notes (learned the hard way)

- **LiveConnectConfig `tools=` requires a LIST of types.Tool** (rev 00029
  lesson): passing a single bare Tool dies at connect with
  `AttributeError("'tuple' object has no attribute 'function_declarations'")`
  — session starts, then instantly ends with zero audio. Unit tests that
  only check construction miss this; test the list shape.
- Native-audio models rarely trigger declared functions on their own;
  system-instruction hints ("for current events … use web_search") made
  triggering reliable on the first probe.

#### Design decisions worth remembering

- **Tavily chosen over Serper/Brave/DDG/Gemini-grounding** (23 Aug):
  agent-native output, existing key, 1k free calls/mo covers personal use;
  query+results flow through OUR code so the audit-log scope is met
  honestly. Gemini native grounding hides usage from our audit path.
- **Reminder delivery decided**: FCM push triggered by Cloud Tasks
  one-shot HTTPS tasks scheduled at each reminder's exact due instant —
  no polling interval at all. Google Calendar rejected for now (OAuth
  restricted-scope bureaucracy, silent token-death failure mode).
- **Audit-first discipline**: unknown-tool calls are audited too, so a
  hallucinated tool name shows up in tool_audit immediately.

### Architecture note

Gemini Live remains the **voice front door**. Tools run in a **server-side agent layer** that receives structured intent from the conversation, not raw PCM.

---

## Phase 5 — Ambient & multi-speaker mode (IN PROGRESS — started 25 Aug 2026)

### Goal

Phone on table during a **group conversation**; Sirious listens to multiple people and helps when asked — closer to the original "assistant in the room" vision.

### Agreed architecture (25 Aug 2026, supersedes the original open questions)

**Side-STT ambient, NOT always-on Gemini.** In ambient mode the mic streams to a
dedicated STT+diarization provider; Gemini Live is connected ONLY on invocation.
Why: (1) Gemini Live cannot diarize — speaker labels are the product; (2) silence
becomes structural (nothing in the room CAN talk) instead of instruction-hoped;
(3) hours of chatter never pollute Gemini's context; (4) transcription-priced cost.

```text
                       ┌─────────────────────────────────────────┐
                       │              PHONE (mic)                │
                       └───────────────┬─────────────────────────┘
                                       │ far-field profile (see device facts)
               ┌───────────────────────┴───────────────────────┐
               │            AMBIENT MODE (C1)                  │
               │   mic ──► STT vendor (streaming WS)           │
               │              └─► diarized turns "S1: …"       │
               │              ──► Firestore ──► History UI     │
               │              ──► invocation spotter (C2)      │
               └───────────────────────┬───────────────────────┘
                    "Sirious" spotted in transcript
                                       │
               ┌───────────────────────┴───────────────────────┐
               │         INVOCATION (reuses Phases 0–4)        │
               │  hot-start Gemini Live seeded with recent     │
               │  transcript tail ──► answers OUT LOUD         │
               │  ──► ducked capture while speaking (C2.5) ──► │
               │      back to ambient                          │
               └───────────────────────────────────────────────┘
```

Normal 1:1 mode (earphones, Gemini Live, Phases 0–4 behavior) is the default and stays
byte-for-byte unchanged.

**Provider decision — REVISED after probes (25 Aug 2026, probes e0bbbd2):**
Google STT v2 **cannot deliver diarization on our path**. Exhaustive matrix
(sync recognize / streaming / batch × chirp_3/long/short/latest_* × global/us/
asia-south1 × en-IN+hi-IN/en-US) → three distinct rejections: sync recognize =
"Recognize does not support Speaker Diarization for the requested model";
streaming = "Recognizer does not support feature: speaker_diarization" (en-US
control too — NOT locale-gating); batch en-US = "Diarization is not currently
supported"; batch chirp_3@us accepted the config but returned EMPTY transcripts
twice. Also learned: chirp_3 exists only in `us`+`asia-south1` (not `global`),
en-IN chirp_3 is allowlist-gated (403 "no longer generally available"), and
feature support is evaluated against the recognizer's OWN model — per-request
model overrides don't count. CONSEQUENCE: Google demoted to fallback
(transcription-only, no diarization). **Launch provider reverts to the original
shortlist: Deepgram (streaming diarization, $200 free credit, no card) or
AssemblyAI (streaming diarization + code-switching, $50 credit) — Paresh to
create accounts and hand over keys.** Marathi caveat: neither streams Marathi;
home Marathi use (small) would need batch or stays on Gemini-only 1:1.
The SttProvider interface + smoke/probe scripts remain valid; only the
GoogleSttProvider's launch role changed. Data-logging/consent points moot for
Google unless it returns as fallback.

### User experience

```text
[Phone face-up on table, ambient toggle ON, recording indicator shown]

Friend: "...what's the longest train in the world?"
User:  "Sirious, can you answer that?"
Sirious: "The longest regularly scheduled passenger train is..."
         [capture ducked while speaking, then back to ambient]

Later, History shows: S1: ... / S2: ... / S1: ...   (long-press → name a speaker)
```

### Chunked working plan (each chunk independently testable)

**C0.5 — Device & audio ground truth — DONE 25 Aug 2026**
- Probed live session: AEC + NoiseSuppression genuinely attach (audio_flinger);
  AGC NOT available on SM-E346B (logcat "Auto gain effect is not available");
  audio source resolves to MIC (not DEFAULT).
- Echo experiment (voiceCommunication + MODE_IN_COMMUNICATION + speakerphone):
  FAILED — Samsung call pipeline hard-gates uplink during playback (digital
  silence, barge-in onset peak=0/floor=0) AND Sirious's own speech leaked through
  playback gaps as ghost user turns. Reverted same day. Full-duplex via call
  pipeline is DEAD on this device. Details: audio_capture_service.dart comment +
  docs/echo_bargein_research.md + sirious-build skill.
- Consequence: answer-time strategy is DUCKING (no exceptions); ambient mode needs
  no echo handling at all (nothing plays).

**C1 — Ambient plumbing — COMPLETE (backend+mobile, 26 Aug 2026, rev 00045).**
Pending: real-table human gate (Paresh expects positive). C2 starts next session.
- Backend: SttProvider interface + DeepgramAmbient (streaming WS, nova-3
  language=multi, diarize, KeepAlive timer vs 1011 idle-close, flat
  "channel" payloads — NOT REST nesting); /ws/ambient endpoint (bearer-auth,
  binary PCM in, ambient_segment events out, ZERO Gemini = structural
  silence); ambient turns persisted via the Phase 2 queue+writer
  (mode=ambient, kind=ambient turns with speaker/text/start_s/end_s);
  GET /sessions/{id} passes ambient turns through in their own shape.
- Tests: 122 pass (7 new: payload parse incl. majority-speaker, grouping).
- E2E local PASS: 11 segments WS + Firestore readback mode=ambient
  speakers=[0,1]. Prod probe PASS (rev 00043): 13 segments, speakers=[0,1]
  over wss on run.app. Probe docs cleaned from Firestore.
- Prod env: DEEPGRAM_KEY added to Cloud Run (rev 00043 deploy set).
- Google STT v2 probes (25 Aug, e0bbbd2): NO diarization on any usable
  path — Google demoted to transcription-only fallback (details above).
- stt_smoke.py / deepgram_smoke.py / ambient_e2e.py / ambient_prod_probe.py
  are the verification chain for any future provider swap.
- ON-DEVICE tests (26 Aug): (1) solo voice test — chain works, but exposed
  interim duplicates (fixed: finals-only) and single-speaker S0/S1 split
  (relative tags on short utterances — accepted for C1, C3 name-mapping
  absorbs); (2) Perplexity-voice-mode round (53 turns, 2.5 min) — human
  vs machine-speaker diarization clean, Hinglish+Hindi Devanagari clean,
  structural silence held next to a talking loudspeaker. Exposed turn
  fragmentation (TTS micro-pauses → 4 turns/sentence) → fixed with
  server-side turn builder (rev 00045): merge same-speaker segments within
  2 s gap, flush on speaker change / 2.5 s idle / session end; prod probe
  PASS (4 merged turns vs 13 fragments).
- REMAINING in C1: real-table gate — 2–3 humans, office Hinglish, phone on
  table; judge History transcript readability. Then C2 invocation.
- Mobile (DONE 26 Aug): AmbientSessionScreen (equalizer icon on voice screen,
  SafeArea-fixed button), AmbientSessionController (WS client, segments,
  error/done handling), AmbientAudioBridge (far-field CaptureProfile in
  AudioCaptureService — NS/AGC relaxed for ambient), one-time consent screen
  (shared_preferences) + persistent recording indicator, History detail
  renders ambient sessions as speaker-labeled turns with "ambient" badge.
  Voice 1:1 path byte-untouched; flutter analyze clean, widget tests pass,
  release APK on device (build note: --no-tree-shake-icons needed on this PC
  — Windows Application Control blocks font-subset.exe).
- Fork alternatives: (a) if streaming integration is painful → batch-chunk every
  ~30 s through the same provider (laggier, same diarization, simpler); (b) if
  Google streaming Marathi quality disappoints → Deepgram/Assembly batch for
  Marathi as stopgap; (c) provider interface means vendor swap is config-only.
- Gate: Paresh uses it at a real table; transcript readability acceptable.

**C2 — Invocation gate**
- Spot "Sirious" in the ambient transcript stream (case/punct tolerant; STT
  keyterm/phrase boosting pinned to the word where the provider supports it) →
  hot-start a Gemini Live session seeded with recent transcript tail → answer
  through existing playback path → on answer end, back to ambient.
- Fork alternatives (in order if needed): (a) on-device hotword model
  (openWakeWord-class, ~2–5 MB, ~2–5% CPU — NOT heavy, but Flutter integration
  work) if STT mangles the proper noun too often; (b) Silero VAD + hotword combo;
  (c) tap-the-phone-to-invoke as the always-works fallback. Latency note:
  detection choice moves ~300–500 ms of a ~2–4 s pipeline dominated by Gemini
  connect — text-spotting first is the right default.
- Gate: "Sirious, …" answered within a few seconds at a real table; silence
  otherwise (structural, by architecture).

**C2.5 — Answer-time echo handling (ducking ladder)**
1. HARD DUCK first: suppress/discard capture while Sirious speaks + ~300 ms
   post-playback settle (residual echo decay). Deterministic, zero deps.
2. PARTIAL DUCK upgrade: instead of mute, attenuate mic 10–20 dB during playback;
   existing barge-in onset detector + 200–300 ms sustained-speech debounce decides
   real-interrupt-vs-echo. This is the ChatGPT-parity "mostly works, sometimes
   buggy" target. (Idea from Perplexity cross-check, adopted 25 Aug.)
3. AUTO POLICY: wired headset connected → today's full-duplex behavior; loudspeaker
   → ducked mode. AudioManager detection, per-session, no user toggle.
4. ESCALATION ONLY IF 2 ANNOYS: in-app software AEC — SpeexDSP (simpler) then
   WebRTC AEC3 (better) via NDK with playback-as-reference. The labs' way; its own
   mini-phase; not planned unless triggered. Server-side AEC parked permanently
   (hardest variant, no Gemini reference channel).
- Gate: no ghost turns from Sirious's own voice in ambient transcript; (after 2)
  speaker barge-in works most of the time.

**C3 — Manual name mapping**
- Long-press a speaker label in transcript detail → assign name; updates stored
  turns; cascades into memory provenance. No forks; trivial.

**C4 — Memory integration**
- Extraction attributes statements to speakers (fills the null `speaker` field
  from Phase 3 — schema already supports it); recall answers "what did Peter say
  about Mumbai?" with provenance. Fork: if per-speaker extraction quality is poor,
  keep extraction global and store speaker tags only on turns (display-level).

**C5 — Hardening & docs**
- Noisy-room testing, battery/heat on device, cost instrumentation (ambient
  minutes logged per session), protocol docs v3 (ambient mode + events), phase
  gate checklist, then Phase 6 (voiceprint identity) builds on C3's mapping.

### Scope

**In:**
- Explicit invocation model before responding (structural silence in ambient)
- Continuous listening mode with clear UX (recording indicator, consent screen)
- Speaker diarization via dedicated STT provider (not Gemini Live)
- Transcripts tagged by speaker label; optional manual name mapping
- Ducking ladder for answer-time echo (C2.5)

**Out (still):**
- Automatic voice fingerprinting (Phase 6, after C3)
- Face recognition, vision
- Proactive unsolicited interjections
- Full-duplex group barge-in (unless C2.5 step 4 ever triggers)
- Always-on Gemini during ambient (rejected architecture)

### Success criteria

- [ ] Assistant stays silent during ambient conversation until invoked (structural)
- [ ] Transcript distinguishes multiple speakers with acceptable accuracy in quiet room
- [ ] Works with Marathi-dominant home conversation (Paresh's real environment)
- [ ] User can assign names to speaker labels manually
- [ ] No ghost turns from Sirious's own speech in ambient transcripts
- [ ] Normal 1:1 mode shows zero regression

### Risks

- Noisy environments degrade diarization (mitigate: real-table gates per chunk)
- Legal/privacy: recording group conversations — consent UX in C1; provider
  data-logging choice owned by Paresh (logging ON initially, switchable)
- Cost of always-on streaming — bounded: <5 h/month expected ≈ $2–7/mo all-in
- Marathi streaming quality on Google unproven until C1 smoke test
- Far-field capture quality on E346B mics unproven until first real table test
  (C0.5 verified effects attach; room acoustics still unknown)

---

## Phase 6 — People recognition (VOICE & VISION) (FUTURE)

### Goal

Sirious learns **who people are** over repeated interactions — by voice and optionally by sight.

### User experience

```text
First meeting:
  User: "This is Peter."  [Peter speaks]
  User: "And this is Jack." [Jack speaks]

Later meeting:
  [Peter speaks]
  Transcript shows: "Peter: ..."
  [Camera sees Peter — optional]
  Sirious: "Peter is here." (only if user enabled vision)
```

### Scope

**In:**

**Voice (Phase 6a):**

- Enrollment flow: user introduces person + short voice sample
- Voice embedding storage per person
- Match incoming speech segments to enrolled profiles (confidence threshold)
- Graceful fallback: "Unknown speaker" when confidence low

**Vision (Phase 6b):**

- On-demand camera snapshot (not continuous video stream initially)
- Face detection + optional face embedding match against enrolled profiles
- Multimodal context for Gemini when user asks ("who is this?")

**Out:**

- Real-time video understanding
- Recognition without user enrollment/consent
- Surveillance-style always-on camera

### Success criteria

- [ ] After 2–3 enrollment sessions, correctly labels enrolled speakers >80% in quiet 1:1/group settings
- [ ] User can manage enrolled people (add, retrain, delete)
- [ ] Vision is opt-in with clear camera indicator

### Dependencies

- Phase 5 diarization infrastructure (C1 segments + C3 name mapping — the labels
  Phase 6a replaces with enrolled identities)
- Strong privacy/consent framework from Phase 2–3

> **Note (25 Aug 2026):** vendor diarization (Google/Deepgram/Assembly) produces
> speaker *labels*, not reusable voice embeddings — so Phase 6a still needs its own
> embedder (enrollment + matching) regardless of which STT vendor won. The coupling
> to Phase 5 is at the transcript-segment level, not the audio level.

---

## Phase 7 — Team lead & project assistant (FUTURE)

### Goal

Sirious helps manage **people, projects, and tasks** in a leadership context — the original "moving to lead role" use case.

### User experience

```text
During 1:1:
  "Assign the API migration to Priya, due next Friday."

Later:
  "What's the status on Priya's tasks?"
  "Who on the team has bandwidth this week?"
```

### Scope

**In:**

- Task entity linked to people (from Phase 3 memory + Phase 6 identity)
- Project/team structure (manual setup at first)
- Status check-ins via voice
- Integration with task tracker (Linear, Jira, Notion — pick one)
- Briefings: "Summarize what happened in today's meetings"

**Out:**

- HR performance management
- Autonomous delegation without confirmation

### Success criteria

- [ ] Tasks created via voice appear in chosen task system
- [ ] User can query task status by person or project
- [ ] Briefings draw from stored transcripts + memory accurately

---

## What NOT to build yet

Regardless of excitement about the north star, **do not start** these until their phase gate is met:

| Feature | Wait until |
|---------|------------|
| Voice fingerprinting (Peter/Jack) | Phase 6, after Phase 5 diarization + C3 name mapping |
| In-app software AEC (SpeexDSP/AEC3) | C2.5 step 4 — only if partial-duck barge-in annoys in daily use |
| Camera / face ID | Phase 6b, after voice identity works |
| Long-term memory | Phase 3, after Phase 2 persistence |
| Calendar/email/tools | Phase 4, after Phase 3 memory |
| Multi-speaker table mode | Phase 5, after Phase 1 mobile is solid |
| Autonomous agent | Phase 7, after tools are reliable |
| Rewriting `main.py` / test client | Only for protocol bugs or new phase requirements |
| 8-minute session auto-resume | Phase 2 (before long meeting use cases) |

---

## Phase gate checklist

Before moving to the next phase, confirm:

1. **Success criteria** for current phase are met on a **real device** (not just laptop/dev).
2. **No critical regressions** in voice quality, latency, or barge-in.
3. **Protocol and docs** updated for any server/client contract changes.
4. **Scope of next phase** agreed — what's in and explicitly out.

---

## Recommended timeline (indicative)

These are engineering estimates for a solo/small team, not commitments.

| Phase | Focus | Rough effort |
|-------|-------|--------------|
| 0 | Voice pipe | Done |
| 1 | Flutter mobile client | 2–4 weeks |
| 2 | Transcript persistence | 2–3 weeks |
| 3 | Memory extraction & retrieval | 4–6 weeks |
| 4 | Tools (2–3 integrations) | 4–6 weeks |
| 5 | Multi-speaker ambient mode | 6–10 weeks |
| 6 | Voice/vision identity | 8–12+ weeks |
| 7 | Team/project assistant | Ongoing |

Phases 5–7 are research-heavy. Expect iteration and partial delivery within each phase.

---

## Mapping original vision → phases

| Original idea | Phase |
|---------------|-------|
| Voice conversational assistant | 1 |
| Works on mobile (primary) | 1 |
| Works on laptop | 1 (Flutter desktop or keep Python dev client) |
| Interrupt assistant while speaking | 1 |
| Remembers conversations | 2 (raw) → 3 (semantic) |
| Brainstorm alone from past talks | 2 |
| Silent unless asked | 1 (basic) → 5 (ambient group) |
| Listen to group on table | 5 |
| Introduce Peter/Jack by name | 5 (manual) → 6 (voice learning) |
| Learn voice/tone over time | 6 |
| Vision / identify people by sight | 6b |
| Manage team tasks | 7 |
| Complete context like a team member | 3 + 5 + 7 (accumulated) |
| Take actions (tools) | 4 |

---

## One-line summary

> **Ship Phase 1 (mobile voice loop with real barge-in) before anything else. Then persistence, then memory, then tools. Multi-speaker, identity, and vision are separate hard problems — pursue them only after the personal assistant is reliable and useful on your phone every day.**
