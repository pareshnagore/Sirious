import 'dart:async';
import 'dart:typed_data';

import 'package:permission_handler/permission_handler.dart';
import 'package:record/record.dart';

import '../config/app_config.dart';

typedef AudioChunkHandler = void Function(Uint8List chunk);

/// Capture tuning per session mode (Phase 5).
///
/// - nearTalk: the 1:1 voice-loop profile — full NS/AGC for close speech.
/// - farField: ambient/table profile. NS is relaxed so distant voices are
///   not attenuated; AGC is a no-op on SM-E346B (effect unavailable, probed
///   25 Aug) but declared false to match intent. Source stays MIC — the
///   voiceCommunication experiment was decisively rejected on this device.
enum CaptureProfile { nearTalk, farField }

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

  Future<void> start({CaptureProfile profile = CaptureProfile.nearTalk}) async {
    await stop();

    final hasPermission = await ensurePermission();
    if (!hasPermission) {
      throw StateError('Microphone permission denied');
    }

    final farField = profile == CaptureProfile.farField;

    final stream = await _recorder.startStream(
      RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: AppConfig.inputSampleRate,
        numChannels: AppConfig.inputChannels,
        // Near-talk (1:1): on-device AEC/NS so the mic does not feed
        // Sirious's own playout back to the server. Without this, earpiece/
        // speaker audio bleeds into the capture, which causes audible echo
        // AND confuses Gemini's barge-in detection.
        //
        // Far-field (ambient): NS relaxed — aggressive suppression tuned for
        // near speech attenuates table-distance voices, hurting exactly the
        // far-field capture diarization needs. Nothing plays out in ambient
        // mode, so there is no echo to cancel.
        echoCancel: !farField,
        noiseSuppress: !farField,
        autoGain: !farField,
        // NOTE (25 Aug echo experiment, REVERTED): voiceCommunication source +
        // MODE_IN_COMMUNICATION + speakerphone was tested for full-duplex
        // speaker use. Result on SM-E346B: uplink delivers DIGITAL SILENCE
        // during playback (barge-in onset peak/floor = 0, "onset missed") and
        // echo still leaked through playback gaps as ghost user turns.
        // Samsung's call pipeline hard-gates the mic during far-end audio.
        // Conclusion: full-duplex via call pipeline is a dead end on this
        // device — ducking (suppress capture while Sirious speaks) is the
        // only viable answer-time strategy. See sirious-build skill.
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
