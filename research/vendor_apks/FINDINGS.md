# Vendor APK teardown — ChatGPT & Perplexity audio stacks (evidence)

Date: 30 Aug 2026. Method: pulled installed base + arm64 split APKs from the
device (adb), scanned DEX class/string pools and native `.so` string tables.
Evidence = literal strings/classes below (paths kept for re-verification).

APKs: research/vendor_apks/{com.openai.chatgpt.apk, openai_arm64.apk,
ai.perplexity.app.android.apk, perplexity_arm64.apk}

## ChatGPT (OpenAI) — com.openai.chatgpt

Native (lib/arm64-v8a/liblkjingle_peerconnection_so.so, LiveKit WebRTC build):
- `AEC3` strings x88 (full WebRTC APM AEC3: `WebRTC-Aec3*` field trials,
  `webrtc::EchoCanceller3`)
- `isAcousticEchoCancelerSupported` (HW AEC probe API)
- `Java_livekit_org_webrtc_audio_JavaAudioDeviceModule_nativeCreateAudioDeviceModule`
  (WebRTC Android audio device module → default AudioSource.VOICE_COMMUNICATION)
- `googEchoCancellation` / `googEchoCancellation: true` (media constraints)

DEX (base.apk):
- `Landroid/media/AudioManager$OnCommunicationDeviceChangedListener;`
- strings: `setCommunicationDevice`, `getAvailableCommunicationDevices`,
  `clearCommunicationDevice`, `getCommunicationDevice`
- `Landroid/media/audiofx/AcousticEchoCanceler;` + `Failed to create the
  AcousticEchoCanceler instance` (WebRTC ADM attaches platform AEC)
- **`activeOutputAudioRmsThresholdRatio` / `ActiveOutputAudioRmsThresholdRatio`
  (repeated as config/data-class fields)** → client-side gate comparing output
  (playback) RMS to a threshold ratio — echo/barge-in gating done ON DEVICE
- Transport: LiveKit SDK (`Llivekit/org/webrtc/*`, ~230 classes)
- Analytics: `ChatgptAbandonedTurnDetected`, `CodexRealtimeTurnStarted`

## Perplexity — ai.perplexity.app.android

Native (lib/arm64-v8a/):
- `libjingle_peerconnection_so.so` (WebRTC): AEC3 x88, AECM note "WebRTC may
  instead use HW AEC if available", `VOICE_COMMUNICATION` ("compressor mode
  %i, using default instead (VOICE_COMMUNICATION)")
- `libsherpa-onnx-c-api.so` + assets `smpl/vad_model_v58.smpl`,
  `smpl/afe_model_no_relu_v58.smpl`, `smpl/afe_params_webrtc_v58_2.json`
  (custom ML VAD + audio front-end models, sherpa-onnx runtime)
- `libonnxruntime.so`, `libmultimodal_uniffi.so` (Rust core via UniFFI)

DEX (base.apk) — their voice2voice stack:
- `GeminiVadSensitivity` / `FfiGeminiVadSensitivity` (Optional in
  `FfiGeminiLiveVoice` constructor with Durations) → **they run Gemini Live
  and configure its VAD sensitivity** (automatic VAD kept, tuned)
- AudioProcessingPipeline with TWO processors:
  - `AudioProcessor$Apm` / `ApmProcessorConfig(echoCancellation=…)` (WebRTC APM)
  - `AudioProcessor$AiCoustics` / `AiCousticsProcessorConfig(enhancement=
    EnhancementLevel, modelId, v1)` → **AiCoustics = commercial neural
    speech-enhancement/AEC** (libaic.so)
- `MicrophoneImpl`, `AudioCommunicationRoutePolicy`,
  `CommunicationRouteMonitor`, error string
  `[Perplexity Assistant] setCommunicationDevice failed for type=` → modern
  communication-device routing (setCommunicationDevice), monitored + policy-driven
- `SmplWebrtcSettings(aecAlgorithm=…, vadMlMethod=…, useEchoCanceller=…,
  useResidualEnhancement=…)` — their AFE config ties WebRTC AEC + ML VAD together
- `USAGE_VOICE_COMMUNICATION` playback usage

## Verdict

Both apps: **VOICE_COMMUNICATION capture + platform AEC first-class, modern
setCommunicationDevice routing, WebRTC software APM available behind it —
and client-side gating/VAD logic that does NOT blindly trust the server.**

- ChatGPT = LiveKit/WebRTC + platform AEC (HW preferred, ADM default) +
  client output-RMS-threshold gate (`activeOutputAudioRmsThresholdRatio`).
- Perplexity = Gemini Live (same API family as Sirious!) + WebRTC APM +
  AiCoustics NEURAL AEC + sherpa-onnx ML VAD + route policy + Gemini VAD
  sensitivity tuning. The heavyweight local front-end, with Gemini's server
  VAD kept but configured.

Caveat: static analysis shows capability + config surface, not guaranteed
runtime config. Live confirmation: run their voice session and read
`dumpsys media.audio_flinger` (source + attached effects) — same probe used
on Sirious 25 Aug.

## Implications for Sirious (Phase 6)

1. Our 25 Aug "platform AEC dead on Samsung" verdict was tested with the
   LEGACY speakerphone path, never with `setCommunicationDevice()` — the
   API both vendors actually drive. Retest before trusting it.
2. Client-side VAD/gating is not optional — it's what OpenAI ships (RMS
   ratio gate) and what Perplexity ships (ML VAD). Gemini server VAD alone
   on raw mic audio is the architecture bug.
3. We do NOT need to leave Gemini Live: Perplexity's stack is literally
   "local front-end + Gemini Live with VAD sensitivity config".
