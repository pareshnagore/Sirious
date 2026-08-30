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

  /// Barge-in onset hard floor (idle) for [route]'s capture path. Platform-AEC
  /// speaker capture is ~10x quieter than the raw near-talk mic (recalibrated
  /// 30 Aug from the probe: room tone ~2-7, near speech 63-120 on the speaker
  /// path vs 1000-8000 raw) — the old 250 would be DEAF on platform capture.
  static double onsetHardFloor(AudioRoute route) =>
      route == AudioRoute.speaker ? 60.0 : 250.0;

  /// Onset hard floor DURING PLAYBACK for [route]. Speaker path: far-end
  /// residual sits ~50 RMS, near speech at table distance 63-120+ → 110.
  /// The 450 stays on the raw-mic path (calibrated for near-talk levels).
  static double onsetPlaybackHardFloor(AudioRoute route) =>
      route == AudioRoute.speaker ? 110.0 : 450.0;
}
