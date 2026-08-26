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

  /// Monotonic activity counter: bumped on every enqueue and every native
  /// feed request. Lets callers detect "playback has gone quiet" without
  /// polling internals (Phase 5 C2 auto-return after an invocation answer).
  int _activityGeneration = 0;
  int get activityGeneration => _activityGeneration;

  /// Number of PCM chunks currently buffered, waiting to be fed to the
  /// native player.
  int get queueLength => _queue.length;

  Future<void> init() async {
    if (_initialized) {
      return;
    }

    await FlutterPcmSound.setLogLevel(LogLevel.error);
    await _setupOutput();
    await FlutterPcmSound.setFeedThreshold(AppConfig.outputSampleRate ~/ 10);
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
    _activityGeneration++;
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

    _activityGeneration++;
    await _drain();
  }

  /// Hard stop: drop queued audio and reset the native player.
  /// Used on barge-in (`interrupted`) and at session end.
  /// Returns the wall-clock time when the speaker has actually gone silent
  /// (after the native player is released), null if not initialized.
  Future<DateTime?> flush() async {
    if (!_initialized) {
      return null;
    }

    _flushing = true;
    _queue.clear();

    DateTime? stopTime;
    try {
      await FlutterPcmSound.release();
      // The speaker is silent the instant the native engine is released.
      // Timestamp it HERE — the re-setup below is extra latency, not silence.
      stopTime = DateTime.now();
      await _setupOutput();
      await FlutterPcmSound.setFeedThreshold(AppConfig.outputSampleRate ~/ 10);
    } finally {
      _flushing = false;
    }

    return stopTime;
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
