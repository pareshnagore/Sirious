class TranscriptTurn {
  const TranscriptTurn({
    required this.userText,
    required this.assistantText,
    this.interrupted = false,
    required this.startedAt,
    this.completedAt,
  });

  final String userText;
  final String assistantText;
  final bool interrupted;
  final DateTime startedAt;
  final DateTime? completedAt;

  TranscriptTurn copyWith({
    String? userText,
    String? assistantText,
    bool? interrupted,
    DateTime? startedAt,
    DateTime? completedAt,
  }) {
    return TranscriptTurn(
      userText: userText ?? this.userText,
      assistantText: assistantText ?? this.assistantText,
      interrupted: interrupted ?? this.interrupted,
      startedAt: startedAt ?? this.startedAt,
      completedAt: completedAt ?? this.completedAt,
    );
  }
}
