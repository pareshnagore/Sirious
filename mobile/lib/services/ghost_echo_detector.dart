/// Stage B lexical ghost-echo detector (extracted for unit testing).
///
/// Why this exists: AEC residual at conversational volume stays intelligible
/// enough that Gemini transcribes the assistant's OWN words back as user
/// turns (seen live 29 Aug on SM-E346B: "What can I help", "Just let me
/// know.", "Just say the word." as You-turns). Energy gating cannot separate
/// speech-echo from speech — echo IS speech — but CONTENT can: the echo is a
/// delayed copy of what was just played.
///
/// The detector keeps a rolling window of recent assistant words and matches
/// incoming user transcripts against it:
///   - 3+ user words: any 3-word shingle present in the assistant window
///   - 2 words: the pair present in the assistant window
///   - 1 word: the word present in the assistant window
///
/// Known, accepted tradeoff: a REAL user who repeats the assistant's words
/// ("let me know when you're free", "stop") within the 30 s window is also
/// dropped once. Bounded cost; revisit if it bites in practice.
class GhostEchoDetector {
  GhostEchoDetector({
    this.windowTtl = const Duration(seconds: 30),
    this.postTurnGuard = const Duration(milliseconds: 3000),
    DateTime Function()? clock,
  }) : _clock = clock ?? DateTime.now;

  final Duration windowTtl;
  final Duration postTurnGuard;
  final DateTime Function() _clock;

  final List<(DateTime, String)> _assistantTexts = <(DateTime, String)>[];
  DateTime _lastTurnCompleteAt = DateTime.fromMillisecondsSinceEpoch(0);

  /// Record a fragment of assistant speech (a transcript delta).
  void trackAssistant(String text) {
    if (text.isEmpty) {
      return;
    }
    final now = _clock();
    _assistantTexts.add((now, text));
    _prune(now);
  }

  /// Call when the assistant's answer finishes (turn_complete).
  void markTurnComplete() {
    _lastTurnCompleteAt = _clock();
  }

  /// True within [postTurnGuard] after the last turn_complete — the window
  /// where the answer's acoustic tail can still arrive through the AEC
  /// residual and open ghost user turns.
  bool get isWithinPostTurnGuard =>
      _clock().difference(_lastTurnCompleteAt) <= postTurnGuard;

  /// True when [userText] repeats the assistant's recent words — i.e. it is
  /// self-echo, not the user.
  bool isEcho(String userText) {
    _prune(_clock());
    final aWords = _words(_assistantTexts.map((e) => e.$2).join(' '));
    if (aWords.length < 2) {
      return false;
    }
    final uWords = _words(userText);
    if (uWords.isEmpty) {
      return false;
    }
    if (uWords.length >= 3) {
      final a3 = <String>{
        for (var i = 0; i + 3 <= aWords.length; i++)
          aWords.sublist(i, i + 3).join(' '),
      };
      for (var i = 0; i + 3 <= uWords.length; i++) {
        if (a3.contains(uWords.sublist(i, i + 3).join(' '))) {
          return true;
        }
      }
      return false;
    }
    if (uWords.length == 2) {
      final a2 = <String>{
        for (var i = 0; i + 2 <= aWords.length; i++)
          aWords.sublist(i, i + 2).join(' '),
      };
      return a2.contains(uWords.join(' '));
    }
    // Single word: exact membership, or a clipped word TAIL — live echo
    // fragments often cut word onsets ('day' from 'today?', 'thing' from
    // 'anything'). Require length >= 3 so stray 2-letter tails don't overfire.
    final w = uWords.first;
    if (aWords.contains(w)) {
      return true;
    }
    return w.length >= 3 && aWords.any((a) => a.endsWith(w));
  }

  void _prune(DateTime now) {
    final cutoff = now.subtract(windowTtl);
    while (_assistantTexts.isNotEmpty &&
        _assistantTexts.first.$1.isBefore(cutoff)) {
      _assistantTexts.removeAt(0);
    }
  }

  static List<String> _words(String s) => s
      .toLowerCase()
      .replaceAll(RegExp(r'[^a-z\u0900-\u097F\s]'), ' ')
      .split(RegExp(r'\s+'))
      .where((w) => w.length > 1)
      .toList();
}
