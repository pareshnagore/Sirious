# Phase 6 — Speaker mode status & next steps (checkpoint 30 Aug 2026)

Handoff note. Where we are, what's proven, what's next. Context for the next
session; also cross-linked from product_phases.md Phase 6 and memory.

## SOLVED: the echo loop (3 days of iterations, root cause found)

The self-echo loop ("app talks to itself after the first answer") is FIXED.

**Final root cause chain (each found by logs/server-dump evidence, in order):**
1. **Barge-in flush destroyed the AEC** — every `flush()` called
   `FlutterPcmSound.release()` + re-setup, shifting playout latency
   (delay est 84→300 ms) and collapsing suppression (21.3→2.9 dB) permanently.
   → **Soft flush** (drop queue, stop feeding; never release mid-session):
   flush 0–3 ms, est stays 84 ms, suppression survives 10+ flushes.
2. **Software AEC was the wrong architecture entirely** — vendor apps don't
   fight echo in software; they use Android's PLATFORM AEC. APK teardown of
   ChatGPT + Perplexity (evidence: research/vendor_apks/FINDINGS.md):
   - Both: WebRTC (`libjingle_peerconnection_so.so`, AEC3 x88) +
     `VOICE_COMMUNICATION` capture + `setCommunicationDevice` routing +
     `AcousticEchoCanceler` (platform).
   - ChatGPT: LiveKit SDK + client gate `activeOutputAudioRmsThresholdRatio`.
   - Perplexity: Gemini Live + WebRTC APM + AiCoustics NEURAL AEC + sherpa-onnx
     ML VAD (`vad_model_v58.smpl`) + `GeminiVadSensitivity` config +
     `AudioCommunicationRoutePolicy`/`CommunicationRouteMonitor`.
3. **Platform AEC works on SM-E346B/Android 16** — the 25 Aug "call pipeline
   mutes mic" verdict was WRONG for the modern path; it tested only legacy
   `speakerphone=true` + MODE_NORMAL and misread AEC residual (~11–14 RMS) as
   mute. Probe (PlatformAecProbe.kt, 3 runs): `setCommunicationDevice(SPEAKER)`
   + VOICE_COMMUNICATION + MODE_IN_COMMUNICATION → far-end tone suppressed to
   p50≈50 RMS while near-end voice passes (63–120 vs baseline 2–4). Full-duplex.
4. **On-device confirmation (Paresh, 2 sessions): ZERO echo.** The 3-day
   problem is dead. Follow-ups captured, multi-turn conversation healthy.

**What shipped (commit 6d515ba5):**
- `CaptureProfile.speaker`: voiceCommunication source + MODE_IN_COMMUNICATION +
  speakerphone (record's AndroidRecordConfig) + `set_speaker_comm_device`
  MethodChannel → modern `setCommunicationDevice` in MainActivity.
- Software AEC3 **unwired, not removed** (`_useSoftwareAec=false` in
  SiriousSessionController) — backup, vendor-parity (ChatGPT keeps WebRTC APM
  behind platform AEC). Flip one constant to re-enable.
- Client safety nets retained: soft flush, ghost gate, lexical echo detector,
  route re-init, logs (100-line screen + adb-pullable external dir).
- Onset floors recalibrated for the ~10× quieter platform capture:
  listening 250→60, playback 450→110 (raw-mic-era values were deaf — Paresh's
  real speech read 63–120; "okay stop" gate-rejected as ghost).

## REMAINING GAP: speaker barge-in (only piece left)

Symptoms (30 Aug, post-platform-AEC): mid-answer speech is NOT treated as
barge-in — no `interrupted`, answer plays out, words appear as a user turn
AFTER completion; transcription degraded ("okay stop" → "Rich stop").
Server-side evidence: Gemini transcribed the words only post-turn.
Diagnosis: platform-AEC capture is TOO QUIET — server VAD doesn't fire
mid-playback, and ASR degrades at 63–120 RMS. Client VAD alone cannot fix
transcription quality; gain can.

Earphone flow (the ORIGINAL Gemini Live earphone session) = GREAT, do not touch.

## AGREED IMPLEMENTATION ORDER (next session, Paresh approved 30 Aug)

1. **Route-aware profiles** — auto-detect earphones via the existing
   `route_changed` EventChannel (MainActivity AudioDeviceCallback):
   earphones → old `nearTalk` flow (MODE_NORMAL, MIC source — the "great" one);
   no earphones → `speaker` (platform AEC). Perplexity ships the identical
   pattern (`AudioCommunicationRoutePolicy`). No manual toggle.
2. **Software gain ~4× on speaker capture** (in AudioCaptureService, before WS)
   + check whether platform AGC is active on the VOICE_COMMUNICATION path.
   Fixes BOTH barge-in symptoms: server VAD starts firing mid-answer AND
   "okay stop" transcribes correctly. One session validates with log numbers.
3. **Gemini VAD config**: `realtimeInputConfig.automaticActivityDetection.
   startOfSpeechSensitivity=HIGH` (backend/protocol change, one line).
4. **Client VAD + manual activity signals** (the durable vendor architecture):
   `automaticActivityDetection.disabled=true` → client sends
   `activityStart`/`activityEnd`. VAD = energy gate (residual-aware) +
   duration logic + optional reference correlation; upgrade to ML VAD via
   sherpa-onnx (Perplexity's runtime) if energy+duration is insufficient.
   Docs verified: https://ai.google.dev/gemini-api/docs/live-api/capabilities
   (Hybrid/custom VAD supported); Pipecat ships the same pattern.

Rationale for gain-before-VAD: VAD decides WHEN speech happens; it cannot
improve WHAT the model hears. Tuning VAD on the current quiet signal would
repeat the threshold hell; on a gain-fixed signal it's tractable. Steps 2–3
are cheap and may make step 4 an upgrade instead of a rescue.

## Key references

- Probe code: mobile/android/.../probe/PlatformAecProbe.kt + ProbeActivity.kt
  (transient; report on device external files/platform_aec_probe.log)
- Vendor teardown: research/vendor_apks/FINDINGS.md (binaries untracked)
- Debug briefing (for external AI review): docs/stage_b_debug_briefing.md
- Ghost detector + tests: mobile/lib/services/ghost_echo_detector.dart,
  mobile/test/ghost_echo_detector_test.dart (14 tests on real ghost strings)
- Skill `realtime-audio-echo-cancellation` has the full failure-mode catalog
  (needs a Stage B addendum: platform-AEC discovery).
- Lessons recorded: soft flush (never release native track mid-session),
  thresholds must be recalibrated whenever the capture PATH changes, read FULL
  adb-pulled logs + server /sessions dumps before flashing (never partial
  screenshots), Paresh never needs to barge-in manually for tests (app
  self-barges = the bug signature).
