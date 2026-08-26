/// Data models for the Phase 2 history API.
class SessionSummary {
  SessionSummary({
    required this.id,
    this.title,
    this.preview,
    this.startedAt,
    this.endedAt,
    this.durationS,
    this.turnCount,
    this.model,
    this.updatedMs,
  });

  final String id;
  final String? title;
  final String? preview;
  final String? startedAt;
  final String? endedAt;
  final double? durationS;
  final int? turnCount;
  final String? model;
  final int? updatedMs;

  factory SessionSummary.fromJson(Map<String, dynamic> j) => SessionSummary(
        id: j['id'] as String,
        title: j['title'] as String?,
        preview: j['preview'] as String?,
        startedAt: j['started_at'] as String?,
        endedAt: j['ended_at'] as String?,
        durationS: (j['duration_s'] as num?)?.toDouble(),
        turnCount: j['turn_count'] as int?,
        model: j['model'] as String?,
        updatedMs: j['updated_ms'] as int?,
      );
}

class HistoryTurn {
  HistoryTurn({
    required this.id,
    this.userText = '',
    this.assistantText = '',
    this.interrupted = false,
    this.startedAt,
    this.endedAt,
    this.reason,
    this.isAmbient = false,
    this.speaker,
    this.ambientText = '',
    this.startS,
    this.endS,
  });

  final String id;
  final String userText;
  final String assistantText;
  final bool interrupted;
  final String? startedAt;
  final String? endedAt;
  final String? reason;

  // Ambient (Phase 5) shape — set when isAmbient is true.
  final bool isAmbient;
  final int? speaker;
  final String ambientText;
  final double? startS;
  final double? endS;

  factory HistoryTurn.fromJson(Map<String, dynamic> j) {
    final isAmbient = j['kind'] == 'ambient';
    return HistoryTurn(
      id: j['id'] as String? ?? '',
      userText: j['user_text'] as String? ?? '',
      assistantText: j['assistant_text'] as String? ?? '',
      interrupted: j['interrupted'] as bool? ?? false,
      startedAt: j['started_at'] as String?,
      endedAt: j['ended_at'] as String?,
      reason: j['reason'] as String?,
      isAmbient: isAmbient,
      speaker: (j['speaker'] as num?)?.toInt(),
      ambientText: j['text'] as String? ?? '',
      startS: (j['start_s'] as num?)?.toDouble(),
      endS: (j['end_s'] as num?)?.toDouble(),
    );
  }
}

class SessionDetail {
  SessionDetail({
    required this.id,
    this.title,
    this.model,
    this.device,
    this.startedAt,
    this.endedAt,
    this.durationS,
    this.endReason,
    this.resumeCount = 0,
    this.mode = 'voice',
    this.turns = const [],
  });

  final String id;
  final String? title;
  final String? model;
  final String? device;
  final String? startedAt;
  final String? endedAt;
  final double? durationS;
  final String? endReason;
  final int resumeCount;
  final String mode; // 'voice' | 'ambient'
  final List<HistoryTurn> turns;

  bool get isAmbient => mode == 'ambient';

  factory SessionDetail.fromJson(Map<String, dynamic> j) => SessionDetail(
        id: j['id'] as String,
        title: j['title'] as String?,
        model: j['model'] as String?,
        device: j['device'] as String?,
        startedAt: j['started_at'] as String?,
        endedAt: j['ended_at'] as String?,
        durationS: (j['duration_s'] as num?)?.toDouble(),
        endReason: j['end_reason'] as String?,
        resumeCount: j['resume_count'] as int? ?? 0,
        mode: j['mode'] as String? ?? 'voice',
        turns: ((j['turns'] as List?) ?? const [])
            .map((t) => HistoryTurn.fromJson(t as Map<String, dynamic>))
            .toList(),
      );
}
