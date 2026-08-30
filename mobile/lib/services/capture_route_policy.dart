import 'audio_capture_service.dart';

/// Output-route classification (mirrors the native `get_audio_route` reply:
/// any wired/BT/USB headset → "earphones", otherwise the builtin speaker).
enum AudioRoute { speaker, earphones }

/// Phase 6 Stage C (1): capture-profile policy keyed by AUDIO ROUTE — the
/// production pattern Perplexity ships (AudioCommunicationRoutePolicy +
/// CommunicationRouteMonitor). The capture profile FOLLOWS the route; the
/// user never toggles a mode.
///
/// - earphones/BT connected → [CaptureProfile.nearTalk]: the mic physically
///   cannot hear the speaker, so no AEC is needed; raw-MIC capture gives the
///   best quality/gain and Gemini's server VAD works as-is (the earphone
///   flow has been verified good since Phase 5 — keep it untouched).
/// - builtin speaker → [CaptureProfile.speaker]: the platform-AEC path
///   (VOICE_COMMUNICATION + MODE_IN_COMMUNICATION, probed working 30 Aug —
///   full-duplex echo cancellation with our playout as far-end reference).
abstract final class CaptureRoutePolicy {
  /// Classify a native `get_audio_route` reply. Anything but "earphones"
  /// (null on missing native side, unknown values) defaults to the speaker —
  /// the conservative, echo-safe profile.
  static AudioRoute classifyRoute(String? nativeRoute) =>
      nativeRoute == 'earphones' ? AudioRoute.earphones : AudioRoute.speaker;

  /// The capture profile for [route].
  static CaptureProfile profileForRoute(AudioRoute route) =>
      route == AudioRoute.earphones
          ? CaptureProfile.nearTalk
          : CaptureProfile.speaker;

  /// Capture gain applied on the SPEAKER path (Phase 6 step 2, 30 Aug). The
  /// platform AEC (VOICE_COMMUNICATION) delivers near speech ~10x quieter
  /// than the raw mic — 30 Aug sessions measured barge-in peaks 56–120 vs
  /// the old 110 threshold: client onset missed on quiet attempts (server
  /// VAD carried barge-in alone, ASR degraded to fragments). x4 int16 gain
  /// restores raw-mic-order levels on this path ONLY; earphones are raw mic
  /// already and must NEVER be amplified (keyed to the profile so no caller
  /// can start the speaker path without it).
  static double gainForProfile(CaptureProfile profile) =>
      profile == CaptureProfile.speaker ? 4.0 : 1.0;

  /// Barge-in onset hard floor (idle) for [route]'s capture path, POST-GAIN.
  /// x4 maps pre-gain speech 63–120 → ~250–480, so the speaker floor
  /// converges to the proven raw-mic value (240 ≈ 250) instead of the
  /// pre-gain 60. Earphone (raw mic, gain 1): unchanged 250.
  /// Re-verify from one instrumented session with gain applied before
  /// trusting these (skill rule: recalibrate level-based numbers after every
  /// capture-path change).
  static double onsetHardFloor(AudioRoute route) =>
      route == AudioRoute.speaker ? 240.0 : 250.0;

  /// Onset hard floor DURING PLAYBACK for [route], POST-GAIN. Speaker: x4
  /// maps the pre-gain far-end residual (~50) to ~200 → the pre-gain 110
  /// would sit BELOW residual and false-fire on echo; 450 restores the
  /// raw-mic margin (≈ earphone's proven value). Earphone: unchanged 450.
  static double onsetPlaybackHardFloor(AudioRoute route) =>
      route == AudioRoute.speaker ? 450.0 : 450.0;
}
