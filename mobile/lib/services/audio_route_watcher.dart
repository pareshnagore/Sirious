import 'package:flutter/services.dart';

/// Phase 6 Stage B (B4): audio-route change events from the Android side.
///
/// MainActivity registers an [android.media.AudioDeviceCallback] and emits
/// "route_changed" on the `sirious/audio_route` EventChannel (native-side
/// debounced 500 ms). A route change (speaker ↔ BT ↔ earphone) shifts the
/// acoustic echo path, so the AEC3 delay model must be rebuilt.
///
/// On platforms without the native channel (desktop, non-Android) the stream
/// just never emits — the missing-plugin error is swallowed below so callers
/// can subscribe unconditionally.
class AudioRouteWatcher {
  AudioRouteWatcher._();

  static final AudioRouteWatcher instance = AudioRouteWatcher._();

  static const EventChannel _channel = EventChannel('sirious/audio_route');

  /// Broadcast of route-change events. Subscribe once per controller.
  Stream<void> get routeChanges {
    try {
      return _channel.receiveBroadcastStream().handleError((Object _) {
        // No native implementation (desktop / other platform) — stay silent.
      });
    } catch (_) {
      return const Stream<void>.empty();
    }
  }
}
