import 'dart:async';
import 'dart:collection';

import 'package:flutter/foundation.dart';
import 'package:flutter_pcm_sound/flutter_pcm_sound.dart';

import '../config/app_config.dart';

/// Queues assistant PCM and plays at 24 kHz, with flush on interruption.
///
/// Lifecycle:
/// - [init]   — sets up the native engine **once** (idempotent). The engine is
///   kept warm for the lifetime of the app so follow-up sessions keep playing.
///   We deliberately do NOT `release()` between sessions (the plugin never
///   resets its `_needsStart` flag, so release+setup silently kills the next
///   session's audio while JSON events keep flowing).
/// - [enqueue]— buffers PCM and kicks the drain loop by feeding directly, so
///   we never depend on the plugin's fragile `_needsStart`-gated `start()`.
/// - [flush]  — barge-in / session end: drop pending audio and hard-stop the
///   native player (release + re-init). Safe to call repeatedly.
/// - [dispose]— app teardown only.
class AudioPlaybackService {
  final Queue<Uint8List> _queue = Queue<Uint8List>();
  bool _initialized = false;
  bool _flushing = false;
  bool _draining = false;

  Future<void> init() async {
    if (_initialized) {
      return;
    }

    await FlutterPcmSound.setLogLevel(LogLevel.error);
    await _setupOutput();
    await FlutterPcmSound.setFeedThreshold(
      AppConfig.outputSampleRate ~/ 10,
    );
    FlutterPcmSound.setFeedCallback(_onFeedRequested);

    _initialized = true;
  }

  Future<void> _setupOutput() async {
    await FlutterPcmSound.setup(
      sampleRate: AppConfig.outputSampleRate,
      channelCount: AppConfig.outputChannels,
      iosAudioCategory: IosAudioCategory.playAndRecord,
    );
  }

  void enqueue(Uint8List pcm) {
    if (!_initialized || _flushing || pcm.isEmpty) {
      return;
    }

    _queue.add(pcm);
    unawaited(_drain());
  }

  Future<void> _drain() async {
    if (_draining || !_initialized) {
      return;
    }

    _draining = true;
    try {
      while (_queue.isNotEmpty && !_flushing) {
        final chunk = _queue.removeFirst();

        try {
          await FlutterPcmSound.feed(
            PcmArrayInt16(bytes: ByteData.sublistView(chunk)),
          );
        } catch (error, stackTrace) {
          debugPrint('AudioPlaybackService feed error: $error\n$stackTrace');
          break;
        }
      }
    } finally {
      _draining = false;
    }
  }

  Future<void> _onFeedRequested(int remainingFrames) async {
    if (_flushing || !_initialized) {
      return;
    }

    await _drain();
  }

  /// Hard stop: drop queued audio and reset the native player.
  /// Used on barge-in (`interrupted`) and at session end.
  Future<void> flush() async {
    if (!_initialized) {
      return;
    }

    _flushing = true;
    _queue.clear();

    try {
      await FlutterPcmSound.release();
      await _setupOutput();
      await FlutterPcmSound.setFeedThreshold(
        AppConfig.outputSampleRate ~/ 10,
      );
    } finally {
      _flushing = false;
    }
  }

  /// App-teardown only. Not called between sessions.
  Future<void> dispose() async {
    _queue.clear();
    _draining = false;

    if (_initialized) {
      await FlutterPcmSound.release();
      _initialized = false;
    }
  }
}
