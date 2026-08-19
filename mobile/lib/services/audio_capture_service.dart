import 'dart:async';
import 'dart:typed_data';

import 'package:permission_handler/permission_handler.dart';
import 'package:record/record.dart';

import '../config/app_config.dart';

typedef AudioChunkHandler = void Function(Uint8List chunk);

/// Captures microphone PCM at 16 kHz mono for protocol v1.
class AudioCaptureService {
  AudioCaptureService({required this.onChunk});

  final AudioChunkHandler onChunk;
  final AudioRecorder _recorder = AudioRecorder();

  StreamSubscription<Uint8List>? _subscription;

  Future<bool> ensurePermission() async {
    final status = await Permission.microphone.request();
    return status.isGranted;
  }

  Future<void> start() async {
    await stop();

    final hasPermission = await ensurePermission();
    if (!hasPermission) {
      throw StateError('Microphone permission denied');
    }

    final stream = await _recorder.startStream(
      RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: AppConfig.inputSampleRate,
        numChannels: AppConfig.inputChannels,
        // Enable on-device acoustic echo cancellation / noise suppression so
        // the mic does not feed Sirious's own playout back to the server.
        // Without this, earpiece/speaker audio bleeds into the capture, which
        // causes audible echo AND confuses Gemini's barge-in detection.
        echoCancel: true,
        noiseSuppress: true,
        autoGain: true,
      ),
    );

    _subscription = stream.listen(onChunk);
  }

  Future<void> stop() async {
    await _subscription?.cancel();
    _subscription = null;

    if (await _recorder.isRecording()) {
      await _recorder.stop();
    }
  }

  Future<void> dispose() async {
    await stop();
    await _recorder.dispose();
  }
}
