import 'package:flutter_test/flutter_test.dart';
import 'package:sirious/services/ambient_session_controller.dart';

void main() {
  group('isInvocationText', () {
    test('matches the product name case-insensitively', () {
      expect(AmbientSessionController.isInvocationText('sirious'), isTrue);
      expect(AmbientSessionController.isInvocationText('Sirious'), isTrue);
      expect(
        AmbientSessionController.isInvocationText(
          'SIRIOUS, can you answer that?',
        ),
        isTrue,
      );
    });

    test('tolerates punctuation around the word', () {
      expect(
        AmbientSessionController.isInvocationText('hey sirious! answer please'),
        isTrue,
      );
      expect(
        AmbientSessionController.isInvocationText(
          '(sirious) what is the longest train?',
        ),
        isTrue,
      );
    });

    test('accepts common phonetic STT variants', () {
      expect(AmbientSessionController.isInvocationText('hey siryus'), isTrue);
      expect(
        AmbientSessionController.isInvocationText('sirius, tell me'),
        isTrue,
      );
    });

    test('REJECTS the everyday word "serious" (false-positive minefield)', () {
      expect(
        AmbientSessionController.isInvocationText('I am being serious'),
        isFalse,
      );
      expect(AmbientSessionController.isInvocationText('serious'), isFalse);
      expect(
        AmbientSessionController.isInvocationText('main serious hoon'),
        isFalse,
      );
    });

    test('rejects empty and unrelated text', () {
      expect(AmbientSessionController.isInvocationText(''), isFalse);
      expect(
        AmbientSessionController.isInvocationText('what is the weather'),
        isFalse,
      );
      expect(AmbientSessionController.isInvocationText('curious'), isFalse);
    });
  });

  group('invocationTail', () {
    AmbientSegment seg(int n) => AmbientSegment(
      speaker: n % 2,
      text: 'segment $n',
      startS: n.toDouble(),
      endS: n + 1.0,
    );

    test('returns all segments when under the cap, minus the trigger', () {
      final all = [seg(0), seg(1), seg(2)];
      final trigger = all[1];
      final tail = AmbientSessionController.invocationTail(all, trigger, 8);
      expect(tail.map((s) => s.text), ['segment 0', 'segment 2']);
    });

    test('caps to the most recent N segments before dropping the trigger', () {
      final all = [for (var i = 0; i < 12; i++) seg(i)];
      final trigger = all[11];
      final tail = AmbientSessionController.invocationTail(all, trigger, 8);
      // sublist(12-8=4) = segments 4..11 (8 items), then the trigger (11) is
      // excluded → 7 room-context lines to seed Gemini with.
      expect(tail.length, 7);
      expect(tail.map((s) => s.text), [
        'segment 4',
        'segment 5',
        'segment 6',
        'segment 7',
        'segment 8',
        'segment 9',
        'segment 10',
      ]);
    });
  });
}
