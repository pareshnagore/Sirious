import 'package:flutter_test/flutter_test.dart';
import 'package:sirious/services/ghost_echo_detector.dart';

/// Fixtures from the LIVE failure of 29 Aug (server-side session dump,
/// cs-mteov7ic-ebcss): Gemini transcribed the assistant's own AEC residual
/// as user turns. The detector must classify each of those as echo.
void main() {
  group('GhostEchoDetector — real ghost strings from 29 Aug session', () {
    late GhostEchoDetector d;

    /// Assistant speech exactly as the live session produced it (turn order).
    void feedAssistant() {
      d.trackAssistant(
        'I aim to be helpful and concise. What can I do for you today?',
      );
      d.trackAssistant('What can I help you with today?');
      d.trackAssistant("Is there anything specific you'd like to discuss?");
      d.trackAssistant("You'd like to discuss? Just let me know.");
      d.trackAssistant("Alright. Just say the word if you need anything.");
      d.trackAssistant("I'm here when you need me. I'm here to help.");
      d.trackAssistant(
        "I'm ready to help with whatever you like. Whenever you are.",
      );
      d.trackAssistant('Anything at all— just let me know.');
      d.trackAssistant(
        'Anything else you want— Marvel characters like the Beyonders? Or '
        'anything— or that sort of thing— just say the word. Always ready. '
        "Just ask. What's on your mind?",
      );
    }

    setUp(() {
      d = GhostEchoDetector();
      feedAssistant();
    });

    test('flags the exact ghost user turns seen live', () {
      expect(d.isEcho('What can I help'), isTrue);
      expect(d.isEcho('day'), isTrue);
      expect(d.isEcho('Is there anything'), isTrue);
      expect(d.isEcho("You'd like to"), isTrue);
      expect(d.isEcho('Just let me know.'), isTrue);
      expect(d.isEcho('Just say the word.'), isTrue);
      expect(d.isEcho("I'm here."), isTrue);
      expect(d.isEcho('anything at all?'), isTrue);
      expect(d.isEcho('Here to help.'), isTrue);
      expect(d.isEcho('Always ready.'), isTrue);
      expect(d.isEcho('Just ask.'), isTrue);
      expect(d.isEcho('On your mind.'), isTrue);
    });

    test('flagged ghosts include variants of live fragments', () {
      expect(d.isEcho('anything'), isTrue);
      expect(d.isEcho('marvel'), isTrue);
      expect(d.isEcho('or anything'), isTrue);
      expect(d.isEcho('else you want?'), isTrue);
    });

    test('KNOWN MISSES — transcription variance, accepted by design', () {
      // Gemini ghost-transcribes with variance the exact-match detector
      // deliberately does not chase (fuzzy matching would risk eating real
      // user speech). These fall through to the per-turn adaptive duck
      // backstop: one ducked turn, no loop.
      //
      //   'All right.'  — assistant said "Alright" (one word)
      //   'É, OK.'      — assistant said "Okay"; 'ok' is a 2-letter token
      //   "Yes, I'm"    — 'yes' was never spoken (Gemini glue word)
      expect(d.isEcho('All right.'), isFalse);
      expect(d.isEcho('É, OK.'), isFalse);
      expect(d.isEcho("Yes, I'm"), isFalse);
    });

    test('a real NEW user question passes', () {
      expect(d.isEcho("Hey Siri, what's the weather tomorrow?"), isFalse);
      expect(d.isEcho('set a reminder for nine pm'), isFalse);
      expect(d.isEcho('tell me a joke'), isFalse);
    });

    test('a user REPEATING assistant words is also flagged (known tradeoff)',
        () {
      expect(d.isEcho('just let me know'), isTrue);
      expect(d.isEcho('what can I help'), isTrue);
    });
  });

  group('GhostEchoDetector — worded edge cases', () {
    test('empty window never flags', () {
      final d = GhostEchoDetector();
      expect(d.isEcho('what can I help'), isFalse);
    });

    test('one-word assistant history never flags', () {
      final d = GhostEchoDetector()..trackAssistant('hello');
      expect(d.isEcho('hello'), isFalse);
    });

    test('transcript fragments accumulate into matched phrases', () {
      final d = GhostEchoDetector();
      d.trackAssistant('What can');
      d.trackAssistant('I help you');
      expect(d.isEcho('can I help'), isTrue);
    });

    test('single word matches if present anywhere in window', () {
      final d = GhostEchoDetector()
        ..trackAssistant('Marvel characters like the Beyonders.');
      expect(d.isEcho('marvel'), isTrue);
      expect(d.isEcho('beyonders'), isTrue);
    });

    test('three-word shingle across a long window', () {
      final d = GhostEchoDetector()
        ..trackAssistant(
          'I am quite an assistant who does not talk very much at all.',
        );
      expect(d.isEcho('an assistant who'), isTrue);
      expect(d.isEcho('talk very much'), isTrue);
      expect(d.isEcho('purple elephant dreams'), isFalse);
    });

    test('Hinglish/Devanagari words survive tokenisation', () {
      final d = GhostEchoDetector()
        ..trackAssistant('बहुत अच्छा आपके लिए क्या लाऊँ');
      expect(d.isEcho('अच्छा'), isTrue);
      expect(d.isEcho('क्या लाऊँ'), isTrue);
    });

    test('punctuation and case do not matter', () {
      final d = GhostEchoDetector()..trackAssistant('Just Let Me Know.');
      expect(d.isEcho('just let me know'), isTrue);
      expect(d.isEcho('JUST LET ME KNOW!!'), isTrue);
    });
  });

  group('GhostEchoDetector — window TTL and post-turn guard', () {
    test('assistant words expire after windowTtl', () {
      var now = DateTime(2026, 8, 29, 12);
      final d = GhostEchoDetector(clock: () => now);
      d.trackAssistant('what can I help you with');
      expect(d.isEcho('what can I help'), isTrue);
      now = now.add(const Duration(seconds: 31));
      expect(d.isEcho('what can I help'), isFalse);
    });

    test('post-turn guard is true only within the window', () {
      var now = DateTime(2026, 8, 29, 12);
      final d = GhostEchoDetector(clock: () => now);
      d.markTurnComplete();
      expect(d.isWithinPostTurnGuard, isTrue);
      now = now.add(const Duration(milliseconds: 3100));
      expect(d.isWithinPostTurnGuard, isFalse);
    });
  });
}
