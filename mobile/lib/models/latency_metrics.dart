class LatencyMetrics {
  LatencyMetrics();

  DateTime? sessionConnectedAt;
  DateTime? firstMicChunkAt;
  DateTime? firstUserTranscriptAt;
  DateTime? firstAssistantAudioAt;
  DateTime? firstAssistantTranscriptAt;
  DateTime? lastInterruptedAt;

  /// Barge-in measurement (all on the device clock, set by the controller).
  DateTime? bargeInOnsetAt; // T_onset: mic first detected the user's voice
  DateTime? bargeInAudioStoppedAt; // T_stop: speaker actually went silent
  int? lastBargeInServerMs; // interrupted - onset (server detect + network)
  int? lastBargeInFlushMs; // stop - interrupted (client flush)
  int? lastBargeInTotalMs; // stop - onset (what the user perceives)
  bool lastBargeInOnsetMissing = false; // interrupted fired but no onset seen
    double lastBargeInPeakRms = 0; // loudest mic chunk during that playback
    double lastBargeInNoiseFloor = 0; // quiet baseline during that playback

  void resetForTurn() {
    firstUserTranscriptAt = null;
    firstAssistantAudioAt = null;
    firstAssistantTranscriptAt = null;
    bargeInOnsetAt = null;
    bargeInAudioStoppedAt = null;
  }

  String get summary {
    final parts = <String>[];

    if (firstMicChunkAt != null && firstUserTranscriptAt != null) {
      parts.add(
        'mic→transcript: '
        '${firstUserTranscriptAt!.difference(firstMicChunkAt!).inMilliseconds}ms',
      );
    }

    if (firstMicChunkAt != null && firstAssistantAudioAt != null) {
      parts.add(
        'mic→audio: '
        '${firstAssistantAudioAt!.difference(firstMicChunkAt!).inMilliseconds}ms',
      );
    }

    if (firstUserTranscriptAt != null && firstAssistantTranscriptAt != null) {
      parts.add(
        'transcript→text: '
        '${firstAssistantTranscriptAt!.difference(firstUserTranscriptAt!).inMilliseconds}ms',
      );
    }

    if (lastBargeInTotalMs != null) {
      parts.add(
        'barge-in: ${lastBargeInTotalMs}ms '
        '(server ${lastBargeInServerMs}ms + flush ${lastBargeInFlushMs}ms)',
      );
    } else if (lastBargeInOnsetMissing) {
          parts.add(
            'barge-in: interrupted; onset missed '
            '(peak ${lastBargeInPeakRms.toStringAsFixed(0)} · '
            'floor ${lastBargeInNoiseFloor.toStringAsFixed(0)})',
          );
        }

    return parts.isEmpty ? 'No latency samples yet' : parts.join(' · ');
  }
}
