import 'dart:async';

import 'package:flutter/services.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:record/record.dart';

import '../config/app_config.dart';

typedef AudioChunkHandler = void Function(Uint8List chunk);

/// Capture tuning per session mode (Phase 5/6).
///
/// - nearTalk: the 1:1 voice-loop profile — full NS/AGC for close speech.
/// - farField: ambient/table profile. NS is relaxed so distant voices are
///   not attenuated; AGC is a no-op on SM-E346B (effect unavailable, probed
///   25 Aug) but declared false to match intent.
/// - speaker (Phase 6, 30 Aug): PLATFORM AEC path — the configuration the
///   vendor apps (ChatGPT/Perplexity) drive. VOICE_COMMUNICATION source +
///   MODE_IN_COMMUNICATION routes capture through Android's
///   AcousticEchoCanceler WITH a real far-end reference (our playback),
///   giving working full-duplex echo cancellation on SM-E346B/Android 16
///   (probe 30 Aug: far-end tone suppressed to p50≈50 RMS, near-end voice
///   passes during playback at peak 63–120 vs baseline 2–4; legacy
///   MODE_NORMAL+speakerphone path is half-duplex and kills near-end voice).
///   The 25 Aug "call pipeline mutes the mic" verdict tested only the legacy
///   path and misread platform-AEC residual as mute.
/// Stage C (1): the ACTIVE profile is chosen per AUDIO ROUTE by
/// CaptureRoutePolicy (earphones → nearTalk, none → speaker) — callers
/// never hardcode a profile for route reasons.
enum CaptureProfile { nearTalk, farField, speaker }

/// Captures microphone PCM at 16 kHz mono for protocol v1.
class AudioCaptureService {
  AudioCaptureService({required this.onChunk});

  final AudioChunkHandler onChunk;
  final AudioRecorder _recorder = AudioRecorder();

  static const MethodChannel _channel = MethodChannel('sirious/audio_route');

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
    final speakerPlatform = profile == CaptureProfile.speaker;

    if (speakerPlatform) {
      // Modern communication-device routing (belt & suspenders on top of
      // record's speakerphone flag — the same API the vendor apps drive).
      try {
        await _channel.invokeMethod('set_speaker_comm_device');
      } catch (_) {
        // Channel missing / pre-API-31: record's speakerphone flag covers it.
      }
    } else {
      // Undo a previous speaker-mode communication-device pin so playback
      // follows the natural route (earphone/BT) again.
      try {
        await _channel.invokeMethod('clear_comm_device');
      } catch (_) {
        // Channel missing / pre-API-31 — nothing to clear.
      }
    }

    final stream = await _recorder.startStream(
      RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: AppConfig.inputSampleRate,
        numChannels: AppConfig.inputChannels,
        // Near-talk (1:1): on-device AEC/NS so the mic does not feed
        // Sirious's own playout back to the server.
        //
        // Far-field (ambient): NS relaxed — aggressive suppression tuned for
        // near speech attenuates table-distance voices. Nothing plays out in
        // ambient mode, so there is no echo to cancel.
        //
        // Speaker platform (Phase 6): the PLATFORM AEC removes the echo
        // (voiceCommunication source + InCommunication mode routes the
        // capture through AcousticEchoCanceler with our playout as the
        // far-end reference — probed working 30 Aug). The echoCancel flag
        // is irrelevant here: the platform attaches AEC for
        // VOICE_COMMUNICATION unconditionally.
        echoCancel: !farField,
        noiseSuppress: !farField,
        autoGain: !farField,
        androidConfig: speakerPlatform
            ? const AndroidRecordConfig(
                audioSource: AndroidAudioSource.voiceCommunication,
                audioManagerMode: AudioManagerMode.modeInCommunication,
                speakerphone: true,
              )
            : const AndroidRecordConfig(),
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
