class LatencyMetrics {
  LatencyMetrics();

  DateTime? sessionConnectedAt;
  DateTime? firstMicChunkAt;
  DateTime? firstUserTranscriptAt;
  DateTime? firstAssistantAudioAt;
  DateTime? firstAssistantTranscriptAt;
  DateTime? lastInterruptedAt;

  void resetForTurn() {
    firstUserTranscriptAt = null;
    firstAssistantAudioAt = null;
    firstAssistantTranscriptAt = null;
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

    return parts.isEmpty ? 'No latency samples yet' : parts.join(' · ');
  }
}
