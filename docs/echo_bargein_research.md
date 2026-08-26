# Echo & barge-in on loudspeaker — research findings (25 Aug 2026)

**Question:** ChatGPT/Perplexity voice assistants answer over the phone loudspeaker with
barge-in on the same SM-E346B where Sirious needs earphones. They're Play Store apps —
no special permissions. How do they do it, and what can Sirious do?

---

## 1. Calibration: even the labs don't fully solve it

- OpenAI's own AVM FAQ recommends **headphones** to minimize interruptions and states
  voice mode is "not optimized for ... speakerphone".
- Documented bugs: "Real-Time Model is hearing and talking to itself in a loop"
  (community threads), tap-to-interrupt breakage reports, restart-the-app-to-fix-audio
  advice. "Mostly works, sometimes buggy" (Paresh's own description) **is the current
  industry ceiling** on phone speakers.
- So the target is parity: mostly-good speaker answers with usually-working barge-in.

## 2. How the labs actually do it (synthesized from sources below)

Three layers, none of which need special OS privileges:

1. **Software AEC inside the app** — WebRTC's Audio Processing Module (AEC3) compiled
   into the app, fed BOTH the playback stream (reference) and the mic stream,
   time-aligned. Works in MODE_NORMAL; does not need the call pipeline. This is what
   "bypass the platform AEC" means (Forasoft: Android platform AEC quality is "wildly
   inconsistent across OEMs, which is exactly why many serious Android voice apps
   bypass the platform").
2. **Echo-aware turn detection** — after AEC, classify the residual: speech energy left
   → genuine barge-in; near-silence → echo, ignore (Coval's pipeline description).
   Server/model side is also tuned to not trigger on the assistant's own voice.
3. **Huge tuning budgets** — per-device profiles, server-side AEC variants, model
   training on self-voice.

Standard industry recipes (Deepgram docs): Web = `getUserMedia({echoCancellation:true})`;
iOS = AVAudioSession voice processing; Android = VOICE_COMMUNICATION mode — **which we
tested and rejected on this device** (Samsung hard-gates the uplink during playback:
digital silence + leak-through ghost turns, 25 Aug experiment). The labs on Android
effectively use layer 1 (in-app software AEC), not the platform recipe.

## 3. Sirious options, ranked

### A — Ducking (agreed C2 default) ✅
Suppress/discard capture while Sirious speaks; 200–500 ms post-playback settle delay
before resuming (residual echo decay — Coval). Zero deps, deterministic. Cost: no
barge-in during answers. **This is also what IVR-grade systems ship.**

### B — Echo-aware client-side barge-in during duck ("ducking with a peephole") — cheap, try next
Pure Dart, no native code. During duck, keep the existing mic onset detector running
with a HIGHER bar: N consecutive loud frames, spectral/energy check vs the playback
envelope (we know exactly what we're playing). On trigger: flush playback, un-duck,
signal Gemini. This is "poor man's echo-aware barge-in" — how many assistants do it
without full AEC (vocal.com: barge-in needs near-end-to-echo ratio > 0 dB; a raised
threshold approximates that). Failure mode = occasional false triggers from loud
syllables — i.e., exactly ChatGPT's "sometimes buggy". **Recommended C2.5 experiment.**

### C — In-app software AEC (the labs' way) — real fix, real cost
- **WebRTC AEC3** standalone (`webrtc-audio-processing`, what PulseAudio/PJSIP embed):
  best quality; needs NDK/JNI integration of a C++ lib or a prebuilt Android AAR.
- **SpeexDSP AEC**: simpler C lib, easy NDK build, MDF adaptive filter; quality below
  AEC3 but proven (runs on ESP32s). Uses speaker output as reference — we have that
  signal exactly (we render the PCM ourselves via flutter_pcm_sound).
- Benefit beyond echo: cleaned uplink → Gemini hears the user even during playback;
  true full-duplex group mode becomes possible.
- Cost: native build integration + tuning (delay estimation is the hard part —
  20–200 ms variable mobile loop, 1–2 s convergence, double-talk robustness).
  Budget as its own mini-phase. **Do only if B disappoints.**

### D — Server-side AEC (Deepgram's "advanced" route) — park permanently
We control both endpoints, so it's feasible (client uploads mic + playback-reference,
server cancels before Gemini). But time-alignment over a network is the hardest variant
("significantly more complex" — Deepgram), doubles uplink, and Gemini Live has no
reference-channel input. Only if C fails.

## 4. Answers to Paresh's direct questions

- **"Play Store restrictions?"** — Irrelevant here. RECORD_AUDIO, MODIFY_AUDIO_SETTINGS,
  bundling native audio libs are all normal, allowed things. Sirious (sideloaded) has
  MORE freedom, not less. The labs' edge is engineering (layer 1+2 above), not permission.
- **"There must be a way?"** — There is: option C. But the honest finding is that the
  labs' speaker mode is itself "mostly good, sometimes buggy" (§1), and option B can
  reach that same place for far less work.

## 5b. Perplexity cross-check (25 Aug, Paresh's thread) — deltas adopted

Overlapped with our findings (validates them): AEC-with-playback-reference, echo-aware
barge-in classification, half-duplex as the easy start, 200–500 ms post-playback
cooldown, headphones recommendation, "start half-duplex, upgrade later."

**New ideas adopted:**
1. **Partial duck instead of binary mute** — during TTS, reduce mic gain by 10–20 dB
   rather than cutting capture entirely. Weak echo drops below threshold; genuinely
   loud interruptions still register through the attenuation. This becomes the
   concrete mechanism for option B: attenuation + existing onset detector + debounce,
   no separate peephole detector needed.
2. **Device-aware policy** — wired headset connected → full-duplex as today
   (aggressive barge-in); loudspeaker → ducked mode (require stronger sustained
   residual speech). Detectable via Android AudioManager; auto-switch per session.
3. **Debounce/backchannel rule** — interrupt requires sustained speech (~200–300 ms),
   not a single burst; "hmm"/"yeah" alone shouldn't cut playback. Applies to the
   B detector and matches ChatGPT's observed behavior.
4. **Silero VAD** — lightweight on-device VAD worth considering for ambient-mode
   invocation spotting (C2) if the STT vendor doesn't bundle a usable VAD.

Not adopted / already covered: three-loop architecture (we already have capture/WS/
playback separation), full-duplex state machine (our SessionPhase already models it),
Python/WebRTC stack details (wrong platform for us).

## 5. Decision for Phase 5

- C2 answer-time strategy: **ducking (A) as base + B as the barge-in peephole experiment.**
- C stays queued as the "do it like the labs" upgrade if B's false-trigger rate annoys.
- Ambient listening (C1) unaffected — no playback, no echo, no earphones needed.

## Sources

- Coval — Voice AI Echo Cancellation: Causes, Fixes, Best Practices (ducking pipeline,
  echo-aware barge-in classification, 200–500 ms settle, decision matrix, headphones as
  last resort)
- Forasoft — Echo Cancellation on Speakerphones/Bluetooth/AirPods (OEM inconsistency →
  apps bypass platform AEC; reference-signal explanation; Bluetooth mode-switch trap)
- Deepgram docs — Audio Preprocessing & Barge-In deep dive (per-platform recipes,
  server-side AEC complexity warning, external-VAD tradeoff)
- LiveKit blog/docs — agent self-hear = missing client AEC; WebRTC AEC in SDKs;
  voice isolation vs background suppression for far-field/diarization
- OpenAI Help Center AVM FAQ + community threads (headphone recommendation,
  speakerphone not optimized, self-hear loop bugs)
- vocal.com — AEC Barge-In (NER > 0 dB requirement, residual-echo-suppressor vs
  wake-word distortion, double-talk robustness)
- PJSIP AEC guide (WebRTC AEC3 + Speex AEC as swappable software EC implementations)
- gopinaths.gitlab.io — AEC in Android via WebRTC (where AOSP wires the webrtc preproc)
- Medium (Richa Shah) — Flutter WebRTC echo fix: two-layer software+native approach;
  OEMs don't enable HW AEC outside voice-comm config
- SpeexDSP / ESP32-SpeexDSP (AEC-with-speaker-reference on tiny hardware — portability
  proof)
- Own experiment 25 Aug: voiceCommunication + MODE_IN_COMMUNICATION + speakerphone on
  SM-E346B → uplink digital silence during playback + ghost echo turns. Rejected.
  (Details in audio_capture_service.dart comment + sirious-build skill.)
