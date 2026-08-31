import 'package:flutter_test/flutter_test.dart';
import 'package:sirious/services/adaptive_vad_policy.dart';

void main() {
  group('AdaptiveVadPolicy.ambientFloor', () {
    test('min of window (existing controller convention)', () {
      expect(AdaptiveVadPolicy.ambientFloor([12, 5, 22, 9]), 5.0);
    });

    test('empty window → 0 (pre-first-silence state)', () {
      expect(AdaptiveVadPolicy.ambientFloor([]), 0.0);
    });
  });

  group('AdaptiveVadPolicy.listeningOnset — 31 Aug bands', () {
    test('quiet room (ambient 5–22) → 80 clamp; fixed 240 ate 166–249 speech',
        () {
      expect(AdaptiveVadPolicy.listeningOnset(5), 80.0);
      expect(AdaptiveVadPolicy.listeningOnset(14), 80.0);
      expect(AdaptiveVadPolicy.listeningOnset(22), 88.0); // 22×4
      // Quiet speech measured 166–249 must clear every quiet-room onset.
      expect(166, greaterThan(AdaptiveVadPolicy.listeningOnset(22)));
    });

    test('very quiet room: breath-level chunks (30–79) do NOT open a window',
        () {
      // 80 lower clamp exists exactly for this.
      expect(30, lessThan(AdaptiveVadPolicy.listeningOnset(5)));
      expect(79, lessThan(AdaptiveVadPolicy.listeningOnset(5)));
    });

    test('loud room (ambient ~54–72): 150 ceiling governs, speech still clears',
        () {
      // 54.6×4 = 218 → ceiling clamps to 150; measured barge-in speech
      // 247–305 in that room crosses. Ambient×4 binding would only matter
      // above the 166 quiet-speech floor — the ceiling exists so it never
      // climbs there.
      expect(AdaptiveVadPolicy.listeningOnset(54.6), 150.0);
      expect(AdaptiveVadPolicy.listeningOnset(71.8), 150.0);
      expect(247, greaterThan(AdaptiveVadPolicy.listeningOnset(54.6)));
      expect(166, greaterThan(AdaptiveVadPolicy.listeningOnset(71.8)));
    });

    test('extreme ambient clamps at 150 — ceiling below quietest speech (166)',
        () {
      expect(AdaptiveVadPolicy.listeningOnset(40), 150.0);
      expect(AdaptiveVadPolicy.listeningOnset(1000), 150.0);
      expect(166, greaterThan(AdaptiveVadPolicy.listeningOnset(1000)));
    });
  });

  group('AdaptiveVadPolicy.playbackOnset — 31 Aug bands', () {
    test('quiet room: clamps at 150; residual term never below it', () {
      // 01:33 session: floor 5–6, barge-in speech 166–197.
      expect(AdaptiveVadPolicy.playbackOnset(5, 0), 150.0);
      expect(AdaptiveVadPolicy.playbackOnset(6, 12), 150.0);
      // Real quiet barge-ins measured 166/197 cross.
      expect(166, greaterThan(AdaptiveVadPolicy.playbackOnset(6, 12)));
      expect(197, greaterThan(AdaptiveVadPolicy.playbackOnset(6, 12)));
    });

    test('loud room: residual term clears the band that false-fired at 150',
        () {
      // Loud-room residual floor measured 84–168.
      expect(AdaptiveVadPolicy.playbackOnset(20, 84), 168.0);
      expect(AdaptiveVadPolicy.playbackOnset(20, 168), 336.0);
      // The residual itself (≤ p25) never crosses its own ×2 bar…
      expect(84, lessThan(AdaptiveVadPolicy.playbackOnset(20, 84)));
      expect(168, lessThan(AdaptiveVadPolicy.playbackOnset(20, 168)));
      // …but real barge-in speech (255–305, measured 31 Aug 01:09) does —
      // up to residual ≈ 150 (2×150 = 300 < 305).
      expect(305, greaterThan(AdaptiveVadPolicy.playbackOnset(20, 150)));
    });

    test('ambient ceiling: stale loud window cannot push playback past speech',
        () {
      // A stale ambient 70 (loud window) is clamped to 44 → room term 176.
      expect(AdaptiveVadPolicy.playbackOnset(70, 0), 176.0);
      expect(AdaptiveVadPolicy.playbackOnset(1000, 0), 176.0);
      // Quiet windows pass through unchanged.
      expect(AdaptiveVadPolicy.playbackOnset(20, 0), 150.0); // 80 → clamp 150
      // …but real barge-in speech (255–305, measured 31 Aug 01:09) does —
      expect(305, greaterThan(AdaptiveVadPolicy.playbackOnset(70, 0)));
    });

    test('residual seeds: fresh answer uses the last answer residual', () {
      // After a loud answer (residual ~120 window), the NEXT answer starts
      // with playbackOnset(ambient, 120) — not the fixed-150 blind spot.
      final seeded = AdaptiveVadPolicy.playbackOnset(20, 120);
      expect(seeded, 240.0);
      // Residual at/below the seed cannot false-fire.
      expect(120, lessThan(seeded));
    });
  });

  group('AdaptiveVadPolicy.holdLevel — hysteresis', () {
    test('quiet room: word gaps 5–22 hold, true silence (<60) closes', () {
      // 01:34 session: ambient 5, gaps 5–22, listening onset 80.
      final hold = AdaptiveVadPolicy.holdLevel(80);
      expect(hold, 60.0);
      expect(22, lessThan(hold)); // gaps hold
      expect(5, lessThan(hold));
    });

    test('loud room: gaps ~35–90 hold; onset-minus-margin governs', () {
      // Onset 218 (ambient 54.6) → hold 198.4; gaps 35–90 hold.
      final hold = AdaptiveVadPolicy.holdLevel(218.4);
      expect(hold, closeTo(198.4, 0.1));
      expect(90, lessThan(hold));
    });

    test('speech-dip between syllables: hold is onset minus margin', () {
      // The 00:00 failure: windows closed mid-sentence when ONSET was the
      // close bar. Hold must sit clearly below onset in every room.
      final onset = AdaptiveVadPolicy.listeningOnset(14); // 80
      final hold = AdaptiveVadPolicy.holdLevel(onset);
      expect(hold, 60.0);
      expect(hold, lessThan(onset));
    });

    test('playback: hold tracks the LIVE playback decision, not listening',
        () {
      // Loud answer: playback onset rises with residual → hold follows.
      final pOnset = AdaptiveVadPolicy.playbackOnset(20, 168); // 336
      final pHold = AdaptiveVadPolicy.holdLevel(pOnset);
      expect(pHold, closeTo(316, 0.1));
      // Never above the onset it rides.
      expect(pHold, lessThan(pOnset));
    });
  });

  group('AdaptiveVadPolicy.residual — p25 discipline', () {
    test('empty window → 0', () {
      expect(AdaptiveVadPolicy.residual([]), 0.0);
    });

    test('bursts and silence tails do not move the sustained level', () {
      // 1 s = 10 chunks: 7 near-silence tail, 2 sustained echo, 1 burst.
      final window = <double>[8, 10, 9, 110, 120, 115, 9, 8, 4200, 7];
      final p25 = AdaptiveVadPolicy.residual(window);
      expect(p25, lessThan(120)); // burst excluded
      expect(p25, greaterThanOrEqualTo(8)); // sustained, not the min-tail
    });

    test('sustained residual rides at p25, not mean (mean would over-read)',
        () {
      final window = <double>[...List.filled(9, 100.0), 3000.0];
      expect(AdaptiveVadPolicy.residual(window), 100.0);
    });
  });
}
