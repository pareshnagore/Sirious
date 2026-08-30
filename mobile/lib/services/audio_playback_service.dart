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
/// - [flush]  — barge-in / session end: drop pending audio and stop feeding.
///   SOFT by design (Phase 6 Stage B): the native track is NOT released —
///   releasing + re-setting up on every barge-in changed the playout path
///   latency (AEC delay est 84→300 ms measured 30 Aug) and permanently broke
///   the AEC3 echo model for the rest of the session (21.3→2.9 dB), which
///   resurrected the self-echo loop. The native buffer only holds ~100 ms,
///   so stopping the feed silences the speaker in ≤0.2 s with the audio
///   track (and the AEC's view of it) untouched.
/// - [dispose]— app teardown only (the ONLY place the track is released).
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

  /// Phase 6 Stage A: tap fired immediately before each chunk goes to the
  /// native player (AEC far-end reference). Set by the session controller.
  void Function(Uint8List chunk)? playbackTap;

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

        // Phase 6 Stage A: render reference is fed HERE — immediately before
        // the native feed, not at enqueue time — so the AEC's far-end
        // timeline matches actual playout as closely as possible (the queue
        // can add 0.5-2s of jitter that breaks AEC3's delay search).
        playbackTap?.call(chunk);

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

  /// Stop playback on barge-in / session end.
  ///
  /// HARD (default): drop the queue AND release the native player — the
  /// speaker goes silent the instant release() runs. This is the Phase 1-4
  /// behavior (verified good on the earphone path) and is now the default
  /// for every route while the software AEC3 is UNWIRED: with PLATFORM AEC
  /// as the echo canceller there is no client-side delay model to protect,
  /// so the old reason for soft flush (below) no longer applies.
  ///
  /// SOFT (hard: false): drop queued audio and stop feeding, native track
  /// untouched. ONLY for when the software AEC3 pipeline is re-enabled
  /// (_useSoftwareAec): releasing + re-setting up on barge-in changes the
  /// playout-path latency and permanently invalidates the AEC3 delay model
  /// mid-session (measured 30 Aug: est 84→300 ms, reduction 21.3→2.9 dB).
  /// Soft stop: drop queued audio and STOP FEEDING, but do NOT release the
  /// native player (see the class doc — releasing on barge-in broke the AEC
  /// delay model for the rest of the session). The native feed buffer holds
  /// only ~100 ms after the last feed, so the speaker goes silent quickly.
  /// Returns the wall-clock time the flush ran — an upper bound on when the
  /// speaker actually goes silent (≤~200 ms later, buffer drain).
  Future<DateTime?> flush({bool hard = true}) async {
    if (!_initialized) {
      return null;
    }

    _flushing = true;
    _queue.clear();

    if (!hard) {
      // SOFT: let an in-flight _drain() observe _flushing and bail before
      // its next feed — at most one extra chunk (~10-40 ms) plays past
      // this point, and the native buffer drains to silence shortly after.
      await Future<void>.delayed(Duration.zero);
      final softStop = DateTime.now();
      _flushing = false;
      return softStop;
    }

    // HARD: release the native engine — silence is immediate. Timestamp
    // HERE; the re-setup below is extra latency, not silence.
    DateTime? stopTime;
    try {
      await FlutterPcmSound.release();
      stopTime = DateTime.now();
      await _setupOutput();
      await FlutterPcmSound.setFeedThreshold(
        AppConfig.outputSampleRate ~/ 10,
      );
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
