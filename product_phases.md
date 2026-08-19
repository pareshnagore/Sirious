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

## Current status

**Phase 0 is largely complete.**

```text
✅ Cloud Run WebSocket bridge
✅ Gemini Live native audio loop
✅ Streaming mic in / audio out
✅ Input & output transcription
✅ Turn detection & Gemini barge-in detection
✅ Structured server logging & turn summaries
🟡 Session resumption (detected, not automated)
🟡 Flutter mobile client builds & runs on device (Phase 1 in progress)
❌ Persistent storage
❌ Memory, tools, multi-speaker, vision
```

**Active focus:** Phase 1 — real mobile client with proper audio architecture.

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
🟡 Barge-in latency measured on device (target ~200–500 ms)
🟡 Full "smooth voice experience" acceptance on device
❌ Release APK (signed); currently debug
❌ Upstream protocol/docs re-verification as needed
```

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

## Phase 1 — Personal voice assistant on mobile (NOW)

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

## Phase 2 — Session history & review (NEXT)

### Goal

Conversations are **saved and searchable**. User can revisit what was said and brainstorm alone later.

### User experience

- After a session, user sees a transcript timeline
- Can search past sessions ("what did we discuss about Mumbai?")
- Can start a new session with optional reference to past context

### Scope

**In:**

- Persist `turn_summary` data (user text, assistant text, timestamps, interruption flags)
- Session metadata (start/end, device, duration)
- Storage backend (Firestore, Cloud SQL, or BigQuery — choose one)
- Async write path (must not block real-time WebSocket)
- Simple in-app history list + transcript detail view
- Server-side session resumption on `go_away` (needed for sessions > ~8 min before this matters in production)

**Out:**

- Semantic memory extraction
- Speaker attribution
- Tool execution
- Audio recording archival (text-first; optional audio later)

### Success criteria

- [ ] Every completed turn persisted within seconds of `turn_complete`
- [ ] User can open a past session and read full transcript
- [ ] Sessions longer than 8 minutes work via server-side resumption
- [ ] No data loss on normal disconnect

### Dependencies

- Phase 1 client working
- Auth (even lightweight) before storing personal data at scale

---

## Phase 3 — Contextual memory (LATER)

### Goal

Sirious **remembers facts about the user, projects, and people** across sessions — not just raw transcripts.

### User experience

```text
Week 1: "I have an interview with Acme next Tuesday."
Week 2: "What should I prepare for my interview?"
Sirious:  "For your Acme interview on Tuesday, you might..."
```

### Scope

**In:**

- Memory pipeline:
  ```text
  turn summaries → episodic store → extraction job → structured memory
  ```
- Memory types:
  - **Episodic:** session/turn references
  - **Semantic facts:** preferences, dates, project details
  - **Entities:** people, companies, projects (names only at first)
  - **Tasks:** action items mentioned in conversation
- Retrieval at session start (inject relevant memory into Gemini system context)
- User controls: view memory, delete incorrect entries
- "Silent assistant" behavior tuning via system instructions

**Out:**

- Voice-based speaker identification
- Automatic task execution
- Vision

### Success criteria

- [ ] Facts mentioned in session N are usable in session N+1 without user re-stating them
- [ ] User can see and delete stored memories
- [ ] Memory injection does not blow context window or add unacceptable latency

### Design principle

**Do not store every transcript line as memory.** Extract selectively. Raw transcript (Phase 2) and durable memory (Phase 3) are separate stores.

---

## Phase 4 — Tools & actions (LATER)

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

- [ ] At least two tools work reliably via voice
- [ ] User is informed before irreversible actions
- [ ] Tool failures degrade gracefully (spoken error, no crash)

### Architecture note

Gemini Live remains the **voice front door**. Tools run in a **server-side agent layer** that receives structured intent from the conversation, not raw PCM.

---

## Phase 5 — Ambient & multi-speaker mode (MUCH LATER)

### Goal

Phone on table during a **group conversation**; Sirious listens to multiple people and helps when asked — closer to the original "assistant in the room" vision.

### User experience

```text
[Friends talking at table. Phone face-up, Sirious listening.]

Friend: "...what's the longest train in the world?"
User:  "Sirious, can you answer that?"
Sirious: "The longest regularly scheduled passenger train is..."
```

### Scope

**In:**

- Explicit **invocation model** (wake phrase, button, or "Sirious" address) before responding
- Continuous listening mode with clear UX (recording indicator, consent)
- **Speaker diarization** (Speaker 1, Speaker 2, …) via separate service — not Gemini Live alone
- Transcripts tagged by speaker label
- Optional manual name mapping: "Speaker 2 is Peter"

**Out (still):**

- Automatic voice fingerprinting
- Face recognition
- Proactive unsolicited interjections

### Success criteria

- [ ] Assistant stays silent during ambient conversation until invoked
- [ ] Transcript distinguishes multiple speakers with acceptable accuracy in quiet room
- [ ] User can assign names to speaker labels manually

### Risks

- Noisy environments degrade diarization
- Legal/privacy: recording group conversations — consent UX required
- Cost of always-on streaming in group settings

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

- Phase 5 diarization infrastructure
- Strong privacy/consent framework from Phase 2–3

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
| Voice fingerprinting (Peter/Jack) | Phase 6, after Phase 5 diarization |
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
