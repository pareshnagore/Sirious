import 'package:flutter/services.dart';

/// Phase 6 Stage B/C: audio-route events + current-route detection from the
/// Android side.
///
/// MainActivity registers an [android.media.AudioDeviceCallback] and emits
/// "route_changed" on the `sirious/audio_route_events` EventChannel (native
/// debounce 500 ms). The CURRENT route is read on demand via the
/// `sirious/audio_route` MethodChannel (`get_audio_route`).
///
/// NOTE: the event channel is `sirious/audio_route_events` — the plain
/// `sirious/audio_route` name is owned by the MethodChannel. Before 30 Aug
/// the watcher listened on the WRONG name, so route events never reached
/// Dart (found 30 Aug while wiring Stage C route-aware profiles).
///
/// On platforms without the native channel (desktop, non-Android) the stream
/// just never emits and [detectRoute] returns null — callers can subscribe
/// unconditionally.
class AudioRouteWatcher {
  AudioRouteWatcher._();

  static final AudioRouteWatcher instance = AudioRouteWatcher._();

  static const EventChannel _eventChannel = EventChannel(
    'sirious/audio_route_events',
  );
  static const MethodChannel _methodChannel = MethodChannel(
    'sirious/audio_route',
  );

  /// Broadcast of route-change events ("route_changed"). Subscribe once per
  /// controller.
  Stream<String> get routeChanges {
    try {
      return _eventChannel
          .receiveBroadcastStream()
          .cast<String>()
          .handleError((Object _) {
        // No native implementation (desktop / other platform) — stay silent.
      });
    } catch (_) {
      return const Stream<String>.empty();
    }
  }

  /// Current output route, classified natively: "earphones" (wired/BT/USB
  /// headset connected) or "speaker". Null when the native side is absent
  /// (desktop / other platform) — callers fall back to the speaker profile.
  Future<String?> detectRoute() async {
    try {
      return await _methodChannel.invokeMethod<String>('get_audio_route');
    } catch (_) {
      return null;
    }
  }
}
