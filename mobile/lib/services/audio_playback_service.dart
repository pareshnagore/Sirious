import 'dart:async';
import 'dart:collection';

import 'package:flutter/foundation.dart';
import 'package:flutter_pcm_sound/flutter_pcm_sound.dart';

import '../config/app_config.dart';

/// Queues assistant PCM and plays at 24 kHz with flush on interruption.
class AudioPlaybackService {
  final Queue<Uint8List> _queue = Queue<Uint8List>();
  bool _initialized = false;
  bool _flushing = false;

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
    FlutterPcmSound.start();
  }

  Future<void> _onFeedRequested(int remainingFrames) async {
    if (_flushing || !_initialized) {
      return;
    }

    while (_queue.isNotEmpty) {
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
  }

  /// Clears queued audio and resets native playback (barge-in).
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

  Future<void> dispose() async {
    _queue.clear();

    if (_initialized) {
      await FlutterPcmSound.release();
      _initialized = false;
    }
  }
}
