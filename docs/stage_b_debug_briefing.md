# Sirious Phase 6 Stage B — debugging briefing (for external review)

Date: 30 Aug 2026. Author: Johnny (Hermes agent). Context: briefing for a
second-opinion AI discussion on the self-echo loop problem.

---

## 1. Product & architecture

- **Goal**: single-user hands-free voice assistant on the phone LOUDSPEAKER
  (phone on table). One normal Gemini Live session, mic open during answers,
  true barge-in. The barge-in/echo problem is the last blocker.
- **Stack**: Flutter app (SM-E346B, Exynos 1330, Android 16). Mic: `record`
  plugin, 16 kHz mono PCM, source = MIC (NOT voiceCommunication — Samsung's
  call pipeline hard-mutes the uplink during playback on this device, tested
  and rejected 25 Aug). Playback: `flutter_pcm_sound`, 24 kHz. Backend: own
  FastAPI WebSocket bridge on Cloud Run → Gemini Live
  (`gemini-3.1-flash-live-preview`). **Gemini's server-side VAD** drives turn
  detection and emits `interrupted` events; the client has no VAD of its own
  beyond an energy-based onset detector for metrics.
- **AEC**: standalone `webrtc-audio-processing` v2.1 (AEC3, M131) built for
  Android via NDK/CMake, thin C ABI, dart:ffi. 10 ms frames; render (24 kHz
  far-end reference) fed at the playback pre-feed tap; render line 100 ms
  advanced by the CAPTURE clock (silence-filled, one render frame per capture
  frame); constant `set_stream_delay_ms(100)`.

## 2. What is PROVEN working (Stage A + this weekend)

- AEC3 tracks this device's playback latency: delay estimate stable at
  **84 ms**, delayValid **100%** over thousands of frames.
- Healthy suppression at conversational volume: **13–28 dB sustained**,
  peaks to 35 dB. Residual floor (p25 post-AEC RMS on echo-carrying frames,
  1 s window): typically **8–30 RMS**.
- Render pipeline design is correct: capture-clocked render line, pre-feed
  tap, silence-fill. When respected, the model locks and stays locked.
- Stage A kill-switch passed: no divergence on this device when fed properly.

## 3. The problem (unsolved)

With capture open during playback, **the assistant's own voice still reaches
Gemini and is transcribed as user turns**, cutting the answer and producing a
self-conversation loop. Observed pattern every session: user asks one thing,
answer starts, then Gemini transcribes echo fragments of its own speech
("What can I help", "Just let me know", "Yep, Ottawa.", "About Marvel?"...)
as user turns, answers them, loops. User cannot get a word in.

### The echo's character (measured, post-AEC)

- Echo arrives in **transient bursts**: individual 100 ms chunks measure
  **1000–8000 RMS** right after near-silence chunks; the residual floor is
  8–15. So ~75% of echo chunks are near-silence and bursts are 1–2 chunks.
- The residual is **intelligible speech** — Gemini transcribes it accurately
  (often verbatim, sometimes paraphrased/re-spelled: "Alright"→"All right",
  "Marvel"→"Marble", invented glue words like "Yes, I'm").
- Echo bursts can run 2+ consecutive chunks (~200 ms), which defeats a
  2-chunk sustained-voice gate.

## 4. Timeline of changes and findings (11 device iterations)

**Yesterday (session 1, 8 builds):**

1. **Un-duck** (removed Stage A's hard duck-everything): loop appeared
   immediately. Server session dump proved the "user" turns were verbatim
   echoes of the assistant's own last sentences.
2. **Adaptive per-turn duck**: on a rejected ghost signal, duck the rest of
   that turn. Hedge based on the wrong premise (energy gates can't detect
   speech-echo); didn't stop the loop, kept as backstop.
3. **Lexical ghost-echo detector**: keep a 30 s window of assistant words;
   any user transcript that repeats them (3-word shingles / bigrams /
   single words / word-tails like "day" from "today") is dropped before it
   can flush playback. Unit-tested against the REAL ghost strings from the
   server dump. Works: many `GHOST_LEXICAL → dropped` lines in logs.
   Does NOT stop the loop because Gemini still HEARS the audio server-side.
4. **Ghost gate on interruption events**: accept `interrupted` only if local
   voice-like onset within 2 s. Broken by design: echo bursts cross the
   energy threshold, so echo manufactures its own "evidence".
5. **Post-turn echo guard** (1.5 s, later 3 s): transcripts right after an
   answer that repeat just-spoken words are dropped. Helps; leaks when echo
   transcripts arrive >3 s after turn end (server-side VAD latency).
6. **Log visibility**: on-screen log 12→100 lines; dual-write to the app's
   external files dir so `adb pull` works on release builds. This is what
   made the real diagnosis possible.

**Today (3 builds):**

7. **Log-driven root cause found**: every barge-in "flush" RELEASED the
   native audio track and re-set it up. First flush of a session shifted
   playout latency (delay est **84→300 ms**) and suppression collapsed
   **21.3→2.9 dB permanently** → the loop. **Soft flush** (drop queue, stop
   feeding; never release mid-session): flush now 0–3 ms, est stays 84 ms,
   suppression **survives 10+ flushes across sessions** (23–28 dB seen).
   This fix is real and verified.
8. **Onset thresholds from measurements**: during playback the hard floor is
   450 (echo bursts 266–521 RMS at low volume; real speech 1000+), residual
   gate residual×8. Partially effective; useless when echo bursts hit
   1000–8000.
9. **Sustained-voice gate**: during playback, a chunk counts as "voice" only
   if the previous chunk was also loud (~200 ms continuous). Echo bursts of
   1 chunk now correctly skipped (BURST_SKIP in log). But 2-chunk echo runs
   still pass; and **this can never fully work** — echo IS speech-like.

**Final test result**: loop persists, though everything client-side works:
lexical drops fire, bursts are skipped, suppression stays healthy (est 84 ms
all session), and in the final session suppression still sometimes fell
(35.6 → 1.2 dB with est stable — see open questions).

## 5. Key numbers

| Metric | Value |
|---|---|
| Delay estimate (healthy) | 84 ms stable, delayValid 100% |
| Delay estimate (after track release — now fixed) | 300 ms, suppression dead |
| Suppression, healthy | 13–28 dB sustained, up to 35 dB |
| Residual floor (p25 post-AEC RMS) | 8–30 typical |
| Echo burst chunks (post-AEC) | 1000–8000 RMS, 1–2 chunks (~200 ms) |
| Real speech at table distance | 1000–8000 sustained (multi-chunk) |
| Onset→interrupted event | ~100–270 ms |
| Flush (soft) | 0–3 ms (was ~30–60 ms + track rebuild) |
| Total onset→speaker-silent | ~100–660 ms |
| Ghost transcripts dropped lexically | many per session (works) |
| Turns per 144 s session (looping) | 38 (vs 2–4 healthy) |

## 6. Structural conclusion

The client can now suppress and *filter* echo effects, but **the echo audio
still streams to Gemini continuously during playback**, and Gemini's
server-side VAD decides on its own. Any pure event-side filtering loses:
you cannot un-hear what the server already heard. The remaining levers are
at the AUDIO-STREAM or VAD-CONFIG level:

### Candidate solutions

1. **Stream gate (half-open duplex)**: don't stream mic frames to Gemini
   while the far end plays UNLESS the client's sustained-voice detector says
   "real local voice". Echo bursts then never reach the server; Gemini can't
   self-interrupt. Cost: real barge-in needs ~200–300 ms of continuous voice
   before the server hears anything (perceived delay ~300–500 ms — probably
   acceptable); risk: if the discriminator misjudges, real barge-in feels
   deaf. Reuses the existing duck plumbing (`duckCapture`) with a dynamic
   trigger instead of a static one.
2. **Client-driven VAD via Gemini Live activity signals**: Gemini Live
   supports `automatic_activity_detection` config with
   `start_of_speech_sensitivity` (HIGH/LOW) and end sensitivity — or
   disabling server VAD and sending explicit activityStart/activityEnd
   realtime events. Client keeps streaming audio but the SERVER only forms
   turns when the client says activity is real. Same discriminator quality
   bar as (1), but no audio is hidden, and LOW start-sensitivity alone might
   reduce echo-triggered false starts.
3. **Reference-correlation discriminator (the principled one)**: we HAVE the
   exact playback signal. For each loud mic chunk during playback, compare
   timing/shape against the reference at the corresponding delay (~84 ms):
   mic-burst coincident with reference-burst → echo; mic energy with
   reference silent → genuine user. AEC3's residual fails at high volumes
   (speaker distortion is nonlinear — unmodelable), but this coarse
   "is the mic loud BECAUSE the speaker is loud" test is robust where the
   linear model breaks. Combine with (1): gate the stream on
   "loud mic AND reference quiet".
4. **Accept half-duplex ducking** (current known-good fallback, already
   implemented): mic streams only in listening phase. No ghost turns ever;
   no mid-answer barge-in (user waits ~5–15 s for the answer to end, or says
   "stop" as a normal turn after). Zero new engineering.
5. **Volume policy**: suppression is strongly volume-dependent (low volume →
   echo near noise floor → up to 35 dB seen; high volume → distortion breaks
   the model → ~2 dB). Mid volume is the design point. A volume sweep and
   possibly a "keep volume ≤ X%" constraint may make ANY of the above work
   dramatically better. Unresolved: why suppression fell to ~2 dB in the
   final session with est stable at 84 ms — need the planned volume-sweep
   measurements (Stage B checklist item, never completed).

### Open questions for the discussion

- Is the ~300–500 ms barge-in latency of a sustained-voice stream gate
  acceptable UX for a table-top assistant?
- Does Gemini Live's `start_of_speech_sensitivity=LOW` meaningfully reduce
  false barge-ins from transients? (Needs a doc check / probe.)
- Why does suppression collapse at high volume even with est locked —
  speaker (nonlinear) distortion, or AEC3 filter divergence from bursts?
  Volume sweep will tell.
- Echo transcripts can arrive in LISTENING phase seconds after the answer
  (server VAD latency) — the lexical guard window may need to extend beyond
  3 s, or drop the window entirely for verbatim repeats (tradeoff: a real
  user repeating the assistant's words verbatim gets eaten once).

## 7. Constraints

- No voiceCommunication source / MODE_IN_COMMUNICATION (Samsung mutes the
  uplink during playback — dead end, proven on this device).
- No WebRTC transport migration; ONE Gemini session; protocol v2 over own WS.
- SM-E346B (mid-range Samsung); user keeps phone on table at arm's length.
- Code state: branch `speaker-mode`, commit `0c3563ac` (all of the above),
  analyze clean, 22/22 tests, release APK on device.
