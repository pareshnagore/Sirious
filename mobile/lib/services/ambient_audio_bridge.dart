import 'dart:async';

import 'package:flutter/foundation.dart';

import 'audio_capture_service.dart';
import 'ambient_session_controller.dart';

/// Phase 5 C1: ties the far-field mic profile to the ambient WS client.
/// Owns capture so the UI talks to ONE controller for ambient mode.
class AmbientAudioBridge {
  AmbientAudioBridge({required this.controller});

  final AmbientSessionController controller;
  AudioCaptureService? _capture;

  Future<void> start() async {
    _capture = AudioCaptureService(onChunk: _sendChunk);
    await controller.start();
    if (controller.phase != AmbientPhase.listening) {
      await _capture?.dispose();
      _capture = null;
      return;
    }
    await _capture!.start(profile: CaptureProfile.farField);
  }

  Future<void> stop() async {
    await _capture?.stop();
    await _capture?.dispose();
    _capture = null;
    await controller.stop();
  }

  void _sendChunk(Uint8List chunk) {
    if (controller.isActive) {
      controller.sendAudio(chunk);
    }
  }

  @visibleForTesting
  static void validateChunkRate(int chunks, Duration elapsed) {
    // 100 ms chunks → ~10 chunks/sec. Exposed for the widget test.
    final rate = chunks / (elapsed.inMilliseconds / 1000);
    assert(rate > 5 && rate < 20, 'mic chunk rate out of band: $rate');
  }
}
