# Sirious Project — Handoff Summary

**Date:** 16 August 2026
**Purpose:** Comprehensive handoff document for a new AI/ChatGPT session to continue development without repeating the investigation already done.

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

The current work is focused on getting the **core real-time voice conversation pipeline** reliable before building memory, tools, mobile UI, etc.

---

# 2. Current architecture

The current architecture is:

```text
                 ┌──────────────────────────┐
                 │       Sirious Client     │
                 │                          │
Microphone ────► │  Python + sounddevice    │
                 │          │               │
                 │          │ PCM 16 kHz    │
                 └──────────┼───────────────┘
                            │
                            │ WebSocket
                            │ WSS
                            ▼
                 ┌──────────────────────────┐
                 │       Cloud Run          │
                 │                          │
                 │ FastAPI / WebSocket      │
                 │        /ws               │
                 │                          │
                 │   main.py                │
                 └──────────┬───────────────┘
                            │
                            │ Gemini Live API
                            ▼
                 ┌──────────────────────────┐
                 │      Gemini Live         │
                 │                          │
                 │ gemini-2.5-flash-        │
                 │ native-audio-preview-    │
                 │ 12-2025                  │
                 └──────────┬───────────────┘
                            │
                            │ audio + transcripts
                            ▼
                 ┌──────────────────────────┐
                 │       Cloud Run          │
                 │                          │
                 │ forwards model audio     │
                 │ + transcription events   │
                 └──────────┬───────────────┘
                            │
                            │ WebSocket
                            ▼
                 ┌──────────────────────────┐
                 │       Sirious Client     │
                 │                          │
                 │ sounddevice speaker      │
                 └──────────────────────────┘
```

## Current technology choices

### Client

Python:

* `asyncio`
* `sounddevice`
* `websockets`

Current test client:

```text
test_continuous.py
```

### Server

Python:

* FastAPI
* Uvicorn
* Google GenAI Python SDK
* Cloud Run

Main server file:

```text
main.py
```

Cloud Run endpoint:

```text
wss://sirious-api-635321277027.asia-south1.run.app/ws
```

Health endpoint:

```text
https://sirious-api-635321277027.asia-south1.run.app/health
```

Cloud Run region:

```text
asia-south1
```

Project:

```text
sirious-2026
```

### Model

Current Gemini Live model:

```python
MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
```

The Gemini Live session is configured with:

```python
response_modalities=["AUDIO"]
```

and both:

```python
input_audio_transcription=types.AudioTranscriptionConfig()
output_audio_transcription=types.AudioTranscriptionConfig()
```

---

# 3. Why this architecture was selected

The important architectural decision was to use **Gemini Live directly for the conversational voice loop** rather than building:

```text
Speech-to-text
    ↓
LLM
    ↓
Text-to-speech
```

as three separate services.

The Live API gives us:

* streaming audio input
* streaming audio output
* input transcription
* output transcription
* conversational state
* interruption/barge-in semantics
* turn detection

This is much closer to the eventual Sirious experience.

The Cloud Run WebSocket server acts primarily as a **thin real-time bridge**:

```text
Client WebSocket
       ↕
FastAPI
       ↕
Gemini Live session
```

rather than putting business logic in the real-time path.

That separation is desirable because later the system can add:

```text
                    ┌── Memory
                    ├── Tools
                    ├── Web search
Client → Live layer ─┼── Personal context
                    ├── Long-term transcript
                    └── Action execution
```

without rebuilding the fundamental audio transport.

---

# 4. Audio format

## Microphone input

Current client:

```python
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_MS = 100
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000
```

Therefore:

```text
16 kHz
mono
int16 PCM
100 ms chunks
```

The server sends this to Gemini as:

```python
types.Blob(
    data=data,
    mime_type="audio/pcm;rate=16000",
)
```

## Assistant audio

Gemini output is currently played at:

```python
OUTPUT_SAMPLE_RATE = 24000
```

with:

```python
dtype="int16"
```

and:

```python
OUTPUT_BLOCKSIZE = 1920
```

1920 samples at 24 kHz is approximately:

```text
80 ms
```

---

# 5. Server implementation

The current `main.py` contains:

## Session management

Each WebSocket connection gets:

```python
session_id = str(uuid.uuid4())
```

and logs:

```text
session_started
session_ended
```

## Turn management

Each conversational turn gets:

```python
turn_id = str(uuid.uuid4())
```

The server tracks:

* `turn_started_at`
* `user_text`
* `assistant_text`
* input audio bytes
* output audio bytes
* generation completion
* turn completion
* interruption

At the end it generates:

```text
turn_summary
```

which is intended to eventually become useful for permanent conversation storage/analytics.

---

# 6. Server concurrency model

Two asynchronous tasks run concurrently:

```python
client_task = asyncio.create_task(
    client_to_gemini()
)

gemini_task = asyncio.create_task(
    gemini_to_client()
)
```

Then:

```python
await asyncio.gather(
    client_task,
    gemini_task,
)
```

So there are two independent streams.

## Client → Gemini

`client_to_gemini()`:

```text
WebSocket receive
       ↓
raw PCM bytes
       ↓
Gemini Live send_realtime_input()
```

It also counts input bytes.

## Gemini → Client

`gemini_to_client()`:

```text
Gemini session.receive()
       ↓
server_content
       ├── input transcription
       ├── output transcription
       ├── model audio
       ├── generation_complete
       ├── interrupted
       └── turn_complete
```

Model audio is forwarded to the client as binary WebSocket messages.

Transcription/control events are sent as JSON.

---

# 7. Current server event protocol

The server sends JSON events to the client including:

```text
session_started
user_transcript
assistant_transcript
response_finished
interrupted
turn_complete
session_warning
session_resumption
error
```

Binary WebSocket messages represent:

```text
assistant PCM audio
```

This distinction is important.

---

# 8. Logging system

A structured logging system was deliberately added.

Every event looks approximately like:

```json
{
  "timestamp": "...",
  "session_id": "...",
  "event": "...",
  ...
}
```

This has been extremely useful for debugging real-time behavior.

Important events:

```text
session_started
turn_started
user_transcript_fragment
assistant_transcript_fragment
generation_complete
interrupted
turn_complete
turn_summary
go_away
session_resumption
client_disconnected
session_ended
```

The `turn_summary` is especially useful.

Example:

```json
{
  "reason": "interrupted",
  "user_text": "...",
  "assistant_text": "...",
  "audio_in_bytes": ...,
  "audio_out_bytes": ...,
  "generation_complete": true,
  "turn_complete": false,
  "interrupted": true
}
```

---

# 9. Things already accomplished

## 9.1 Cloud Run deployment

Completed.

FastAPI application is running on Cloud Run.

Current WebSocket URL:

```text
wss://sirious-api-635321277027.asia-south1.run.app/ws
```

---

## 9.2 Gemini Live connection

Completed.

The server successfully establishes:

```python
client.aio.live.connect(...)
```

with the native audio model.

---

## 9.3 Microphone streaming

Completed.

The client successfully:

```text
Mac microphone
→ 16 kHz mono PCM
→ 100 ms chunks
→ WebSocket
→ Cloud Run
→ Gemini
```

---

## 9.4 Gemini audio response

Completed.

Gemini generates audio and the server streams it back:

```text
Gemini
→ Cloud Run
→ WebSocket binary frames
→ sounddevice
→ Mac speakers
```

---

## 9.5 Input transcription

Completed.

We receive fragments such as:

```text
"Tell"
"me"
"which"
"is"
"the longest train?"
```

This is **expected**.

The Gemini Live transcription API is streaming incremental transcription rather than sending only the final sentence.

The same applies to assistant output transcription.

---

# 10. Important clarification: word-by-word terminal logs

The terminal currently displays things like:

```text
USER: Hey
USER: hi
USER: can
USER: you
USER: tell me about India
```

and:

```text
ASSISTANT: Sure, India
ASSISTANT: is
ASSISTANT: a vast
ASSISTANT: and
...
```

This is because the server/client is logging **transcription fragments**.

This is not evidence that Gemini is generating each word independently.

It is a streaming transcript.

Eventually the client should probably aggregate these fragments:

```text
ASSISTANT: Sure, India is a vast and diverse country...
```

instead of displaying every fragment as a new terminal line.

This is a **UX/logging issue**, not currently a fundamental architecture problem.

---

# 11. Interruption / barge-in investigation

This has been the main debugging area.

Desired behavior:

```text
User: Tell me about India.

Assistant: India is a very large...
           ↓
User interrupts:
"Wait, tell me about Mumbai instead."

Assistant immediately stops speaking.
Assistant responds to Mumbai question.
```

Initially this did not appear to work.

The concern was whether Gemini was failing to detect interruption.

The structured Cloud Run logs eventually established that **Gemini does detect the interruption**.

---

# 12. Critical finding: Gemini interruption works

A representative log sequence showed:

```text
generation_complete
```

for the old answer.

Later:

```text
interrupted
```

was emitted for the same turn.

Then a new turn started.

The new user transcription appeared:

```text
"Wait,"
"wait"
"Tell me what is happening."
```

and Gemini began generating the new response.

Therefore:

```text
Microphone
    ↓
Cloud Run
    ↓
Gemini Live
    ↓
interruption detection
```

is working.

This is a critical conclusion.

**Do not restart the investigation by assuming Gemini's barge-in detection is broken.**

---

# 13. Actual interruption problem identified

The remaining problem is **audio playback cancellation on the client**.

The client currently receives binary audio and directly writes it to:

```python
stream.write(data)
```

Conceptually:

```text
WebSocket
   ↓
ws.recv()
   ↓
audio bytes
   ↓
sounddevice
```

There is no mechanism to remove audio that has already been queued for playback.

Therefore there are two separate events:

### Gemini generation

Gemini can say:

```text
interrupted
```

meaning:

> Stop generating/streaming the old response.

### Local audio playback

The client may already have:

```text
hundreds of milliseconds / seconds
```

of old PCM buffered or queued.

That audio can continue to be heard even after Gemini has cancelled generation.

Therefore:

```text
Gemini interruption ≠ immediate speaker silence
```

unless the client explicitly implements playback cancellation.

---

# 14. Why the current client was not worth continuing to optimize

The current client was a temporary diagnostic client:

```text
test_continuous.py
```

Its purpose was to validate the real-time pipeline.

The eventual product will not use this Python terminal client as its actual UI.

Therefore the decision was made to **stop spending time perfecting playback interruption in this temporary client**.

This is intentional.

The correct eventual mobile/desktop client should implement:

```text
Incoming audio
       ↓
playback buffer
       ↓
audio output
```

with explicit cancellation.

When an:

```json
{"type": "interrupted"}
```

event arrives:

```text
clear playback buffer
stop/flush current audio
start accepting new response audio
```

That should be implemented in the real client later.

---

# 15. The client architecture issue

The current `test_continuous.py` has a direct relationship:

```python
data = await ws.recv()

if isinstance(data, bytes):
    stream.write(data)
```

This means the WebSocket receiver and audio playback are tightly coupled.

A better eventual client architecture is:

```text
                    ┌────────────────────┐
WebSocket receiver ─► audio playback queue│
                    └─────────┬──────────┘
                              ↓
                       audio output
```

with control events capable of flushing the queue.

For example:

```text
WebSocket receiver
       │
       ├── binary audio ─────► queue
       │
       └── interrupted ──────► clear queue
```

A dedicated audio playback worker can then consume the queue.

**This is a future implementation item, not something we need to solve in `test_continuous.py` now.**

---

# 16. The `asyncio.gather()` discussion

The client initially used:

```python
done, pending = await asyncio.wait(
    [mic_task, speaker_task],
    return_when=asyncio.FIRST_COMPLETED,
)
```

This was changed to:

```python
await asyncio.gather(
    mic_task,
    speaker_task,
)
```

The purpose was primarily **session lifecycle handling**.

However, this was not the root cause of the interruption problem.

Changing:

```text
FIRST_COMPLETED
```

to:

```text
gather()
```

does **not** make audio playback interruptible.

This distinction is important for future work.

---

# 18. Cloud Run session timeout / 8-minute issue

A separate issue was observed.

Cloud Run/Gemini eventually generated a:

```text
go_away
```

event with:

```text
time_left: "50s"
```

The relevant session subsequently ended.

This corresponds to the long-lived Gemini Live session lifecycle.

There was discussion about addressing the approximately **8-minute session limitation** using session resumption/reconnection.

The server already contains preliminary support for:

```python
response.session_resumption_update
```

and captures:

```python
update.new_handle
```

when available.

The server logs:

```text
session_resumption
```

and sends the handle to the client.

However, **full automatic reconnection/session-resumption has not been completed and validated**.

---

# 19. Decision regarding the 8-minute issue

The explicit project decision was:

> **Ignore the 8-minute issue for now.**

Do not spend the next development cycle on this.

Reason:

* The core voice loop needs to be validated first.
* The 8-minute lifecycle issue is an operational/session-management problem.
* It is not currently blocking basic voice conversation testing.
* Session resumption can be implemented later.

So status:

```text
8-minute session lifecycle
--------------------------
Detected:          YES
Understanding:     YES
Partial support:   YES
Full solution:     NO
Current priority:  LOW / DEFERRED
```

---

# 20. Session resumption status

The server already has code resembling:

```python
if response.session_resumption_update:

    update = response.session_resumption_update

    if (
        update.resumable
        and update.new_handle
    ):

        log_event(
            session_id,
            "session_resumption",
            handle_received=True,
        )

        await websocket.send_json({
            "type": "session_resumption",
            "handle": update.new_handle,
        })
```

This means the system is at least **observing the resumption handle**.

But this does NOT mean automatic continuation is implemented.

Eventually the architecture needs something like:

```text
Gemini sends GO_AWAY
        ↓
server gets resumption handle
        ↓
server reconnects Live session
        ↓
server supplies previous session handle
        ↓
conversation continues
```

Exact implementation should be verified against the current Gemini Live API documentation when work resumes.

---

# 21. Latency investigation

Latency was also examined.

The current logs give enough information to start measuring:

```text
turn_started
user_transcript_fragment
assistant_transcript_fragment
generation_complete
turn_complete
```

For example, one completed turn had:

```text
turn_started
    ↓
user transcript
    ↓
assistant transcript
    ↓
generation_complete
    ↓
turn_complete
```

The system is capable of calculating:

### Speech-to-first-response latency

```text
first assistant transcript timestamp
-
final/meaningful user input timestamp
```

### Time-to-first-audio

Better metric:

```text
first assistant audio chunk
-
user speech endpoint
```

### Generation duration

```text
generation_complete
-
turn_started
```

### Total turn duration

```text
turn_complete
-
turn_started
```

But **proper client-side perceived latency has not yet been instrumented**.

This should be a future task.

---

# 22. Important distinction for future latency work

Do not confuse:

```text
Gemini generation latency
```

with:

```text
user-perceived audio latency
```

The latter includes:

```text
microphone capture
+
WebSocket transport
+
Gemini VAD / turn detection
+
model processing
+
audio streaming
+
WebSocket transport back
+
client audio buffering
+
speaker output
```

Therefore the future client should timestamp:

```text
T0 = microphone capture
T1 = first server/Gemini user transcription
T2 = first assistant audio received
T3 = first assistant audio actually played
```

Then we can calculate actual latency.

---

# 23. Current major architectural status

The project is now at approximately:

```text
                    STATUS

Cloud Run deployment          ✅
FastAPI WebSocket             ✅
Gemini Live connection        ✅
Microphone streaming          ✅
16 kHz PCM input              ✅
Gemini audio output           ✅
24 kHz playback               ✅
Input transcription           ✅
Output transcription          ✅
Streaming responses           ✅
Turn detection                ✅
Gemini interruption detect    ✅
Server interruption event     ✅
Structured logging             ✅
Turn summaries                 ✅
Session resumption detection  🟡
Automatic session resume       ❌
8-min lifecycle solution      ❌ DEFERRED
Client playback cancellation  ❌ DEFERRED
Production client              ❌
Mobile app                     ❌
Persistent transcript storage  ❌
Long-term memory               ❌
Tool/action system             ❌
Agent orchestration            ❌
```

---

# 24. What should NOT be done next

The new AI should **not** immediately:

1. Rewrite `main.py`.
2. Rewrite `test_continuous.py` repeatedly.
3. Try to solve the 8-minute timeout.
4. Assume Gemini interruption is broken.
5. Re-investigate whether the Live API can detect speech interruption.
6. Spend significant time polishing terminal transcript output.
7. Build memory before the core client architecture is settled.

Those areas have already been investigated enough for the current stage.

---

# 25. What should be done next

The next phase should move away from the temporary terminal client and toward the **actual Sirious client architecture**.

Recommended sequence:

## Step 1 — Freeze the server API

Treat the current FastAPI WebSocket interface as the initial real-time protocol.

Define clearly:

### Binary

```text
client → server
16 kHz mono PCM

server → client
24 kHz mono PCM
```

### JSON

```text
session_started
user_transcript
assistant_transcript
response_finished
interrupted
turn_complete
session_warning
session_resumption
error
```

This becomes the contract between the eventual UI and server.

---

## Step 2 — Build proper client-side audio architecture

The eventual client should separate:

```text
WebSocket transport
```

from:

```text
audio playback
```

Recommended:

```text
                    ┌───────────────┐
                    │ WebSocket     │
                    │ receiver      │
                    └───────┬───────┘
                            │
                 ┌──────────┴──────────┐
                 │                     │
              audio                 control
                 │                     │
                 ▼                     ▼
          Playback Queue         Event Handler
                 │                     │
                 ▼                     │
          Audio Output ◄───────────────┘
```

This is where interruption should eventually be handled.

---

# 26. Step 3 — Build the real UI

The eventual client should probably expose:

```text
┌──────────────────────────────┐
│          Sirious             │
│                              │
│  You: Tell me about India.   │
│                              │
│  Sirious: India is...        │
│                              │
│                              │
│          ● Listening         │
└──────────────────────────────┘
```

But the important part is not the visual UI.

The important client state machine is:

```text
IDLE
  ↓
LISTENING
  ↓
THINKING / RESPONDING
  ↓
PLAYING
  ↓
LISTENING
```

with:

```text
PLAYING
   ↓ user speaks
INTERRUPTING
   ↓
LISTENING
```

The interruption path must be first-class.

---

# 27. Step 4 — Instrument real latency

Once the actual client exists, add timestamps for:

```text
microphone captured
speech detected
user transcript received
assistant generation started
first assistant audio received
first assistant audio played
```

Then measure:

```text
VAD latency
TTFT
network latency
audio buffering latency
end-to-end perceived latency
```

This will be much more useful than optimizing based only on Cloud Run timestamps.

---

# 28. Step 5 — Persistent conversation storage

Once the real-time loop is reliable:

```text
Session
  ├── Turns
  │    ├── user transcript
  │    ├── assistant transcript
  │    ├── timestamps
  │    ├── interruptions
  │    └── metadata
  │
  └── Audio
```

can eventually be persisted.

The existing `turn_summary` logging design is a good starting point for the data model.

---

# 29. Step 6 — Memory

Long-term goal:

```text
raw conversation
       ↓
transcript
       ↓
semantic extraction
       ↓
memory
```

For example:

```text
Conversation:
"I'll be interviewing with X next week."

Memory:
User has an interview with X next week.
```

The memory system should distinguish:

* raw transcript
* episodic conversation
* extracted facts
* preferences
* people/entities
* tasks
* durable user facts

This should **not** be implemented by blindly storing every transcript as memory.

---

# 30. Step 7 — Tools / agent capabilities

Eventually:

```text
Sirious
   ├── conversation
   ├── memory
   ├── web
   ├── calendar
   ├── email
   ├── files
   ├── finance
   └── other actions
```

The Live voice interface should eventually become the **front door to an agent**, rather than the agent itself.

---

# 31. Key design principle going forward

The project should be developed in layers:

```text
Layer 1
Real-time voice transport
        ↓
Layer 2
Conversation / turn management
        ↓
Layer 3
Client UX
        ↓
Layer 4
Persistent conversation
        ↓
Layer 5
Memory
        ↓
Layer 6
Tools
        ↓
Layer 7
Autonomous agent behavior
```

Do not prematurely combine all of these.

The current project has essentially completed most of **Layer 1** and much of the server side of **Layer 2**.

---

# 32. Current known issues

| Issue                              | Status          | Priority |
| ---------------------------------- | --------------- | -------: |
| Basic voice conversation           | Working         |        — |
| Streaming microphone               | Working         |        — |
| Streaming response audio           | Working         |        — |
| Input transcription                | Working         |        — |
| Output transcription               | Working         |        — |
| Gemini turn detection              | Working         |        — |
| Gemini interruption detection      | Working         |        — |
| Immediate local audio cancellation | Not implemented |   Medium |
| Transcript fragment aggregation    | Not implemented |      Low |
| 8-minute session lifecycle         | Known           | Deferred |
| Automatic session resumption       | Not complete    | Deferred |
| Real latency instrumentation       | Not complete    |   Medium |
| Production client                  | Not started     |     High |
| Mobile client                      | Not started     |     High |
| Persistent storage                 | Not started     |    Later |
| Memory                             | Not started     |    Later |
| Tools                              | Not started     |    Later |

---

# 33. Most important conclusions from this conversation

### Conclusion 1

**The core real-time voice pipeline works.**

```text
Mic → WebSocket → Cloud Run → Gemini → Cloud Run → WebSocket → Speaker
```

is operational.

### Conclusion 2

**Gemini Live is detecting user interruptions.**

The logs explicitly show:

```text
interrupted
```

followed by a new conversational turn.

### Conclusion 3

**The observed "assistant doesn't stop" behavior is primarily local audio playback buffering.**

The old audio can continue playing even after Gemini has stopped generating it.

### Conclusion 4

**The Python terminal client is disposable.**

Do not spend excessive time perfecting it.

### Conclusion 5

**The 8-minute session issue is known and intentionally deferred.**

### Conclusion 6

**The next meaningful engineering effort should be the actual client architecture**, particularly:

* WebSocket event handling
* audio buffering
* playback cancellation
* interruption UX
* latency measurement

---

# 34. Starting point for the next AI

The next AI should begin with this mental model:

> **We have already proven that Gemini Live + Cloud Run + WebSocket + microphone + speaker works. We have also proven that Gemini detects barge-in. The remaining immediate engineering problem is building the proper client, not repeatedly modifying the temporary Python test client or server.**

The next discussion should therefore start with:

```text
"What should the real Sirious client be?"
```

and decide whether to build it as:

* native mobile,
* Flutter,
* React Native,
* web/PWA,
* or another appropriate client,

while preserving the existing Cloud Run WebSocket contract.

The server should initially remain relatively stable.

---

## 35. Files / components to know

### Backend

```text
main.py
```

Contains:

```text
FastAPI
/ws WebSocket endpoint
Gemini Live connection
client_to_gemini()
gemini_to_client()
turn management
structured logging
session lifecycle
partial session-resumption handling
```

### Temporary client

```text
test_continuous.py
```

Contains:

```text
microphone capture
WebSocket connection
speaker playback
```

It is **not the intended final client**.

### Cloud Run

```text
sirious-api
region: asia-south1
```

Current WebSocket endpoint:

```text
wss://sirious-api-635321277027.asia-south1.run.app/ws
```

---

# 36. One-line project status

> **Sirious has a functioning cloud-based real-time Gemini voice loop with streaming transcription, audio responses, turn detection, structured logging and working Gemini interruption detection; the project is now ready to move from the diagnostic Python client to the real client architecture, while deferring session-resumption/8-minute lifecycle work.**
