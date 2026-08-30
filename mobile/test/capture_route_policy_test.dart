import 'package:flutter_test/flutter_test.dart';
import 'package:sirious/services/audio_capture_service.dart';
import 'package:sirious/services/capture_route_policy.dart';

void main() {
  group('CaptureRoutePolicy.classifyRoute', () {
    test('native "earphones" → earphones', () {
      expect(
        CaptureRoutePolicy.classifyRoute('earphones'),
        AudioRoute.earphones,
      );
    });

    test('null (no native side) and unknown values default to speaker', () {
      expect(CaptureRoutePolicy.classifyRoute(null), AudioRoute.speaker);
      expect(CaptureRoutePolicy.classifyRoute('garbage'), AudioRoute.speaker);
    });
  });

  group('CaptureRoutePolicy.profileForRoute', () {
    test('earphones → nearTalk (raw mic, server VAD works as-is)', () {
      expect(
        CaptureRoutePolicy.profileForRoute(AudioRoute.earphones),
        CaptureProfile.nearTalk,
      );
    });

    test('speaker → platform-AEC speaker profile', () {
      expect(
        CaptureRoutePolicy.profileForRoute(AudioRoute.speaker),
        CaptureProfile.speaker,
      );
    });
  });

  group('CaptureRoutePolicy capture gain (step 2, 30 Aug)', () {
    test('speaker path gains 4x, earphone path stays unity', () {
      expect(CaptureRoutePolicy.gainForProfile(CaptureProfile.speaker), 4.0);
      expect(CaptureRoutePolicy.gainForProfile(CaptureProfile.nearTalk), 1.0);
      expect(CaptureRoutePolicy.gainForProfile(CaptureProfile.farField), 1.0);
    });

    test('gain follows the PROFILE, so route→profile mapping inherits it', () {
      // The speaker ROUTE captures via the speaker PROFILE → gained. The
      // earphone route maps to nearTalk → unity. If the route→profile
      // mapping ever changes, this test flags the gain implication.
      expect(
        CaptureRoutePolicy.gainForProfile(
          CaptureRoutePolicy.profileForRoute(AudioRoute.speaker),
        ),
        4.0,
      );
      expect(
        CaptureRoutePolicy.gainForProfile(
          CaptureRoutePolicy.profileForRoute(AudioRoute.earphones),
        ),
        1.0,
      );
    });
  });

  group('CaptureRoutePolicy onset floors (post-gain)', () {
    test('speaker floors are the POST-GAIN recalibration (30 Aug step 2)', () {
      expect(CaptureRoutePolicy.onsetHardFloor(AudioRoute.speaker), 240.0);
      expect(
        CaptureRoutePolicy.onsetPlaybackHardFloor(AudioRoute.speaker),
        450.0,
      );
    });

    test('earphone floors are the raw-mic calibration (unchanged)', () {
      expect(CaptureRoutePolicy.onsetHardFloor(AudioRoute.earphones), 250.0);
      expect(
        CaptureRoutePolicy.onsetPlaybackHardFloor(AudioRoute.earphones),
        450.0,
      );
    });

    test('platform-AEC x4 gain restores raw-mic-ORDER levels', () {
      // Pre-gain speech measured 63–120 on the speaker path; x4 → 250–480,
      // which must now clear (not sit below) the idle floor.
      expect(
        CaptureRoutePolicy.onsetHardFloor(AudioRoute.speaker),
        lessThan(63.0 * CaptureRoutePolicy.gainForProfile(
          CaptureProfile.speaker,
        )),
      );
    });

    test('speaker playback floor covers the x4-amplified echo residual', () {
      // Pre-gain far-end residual ~50 RMS; x4 → ~200. The playback floor
      // must stay ABOVE that so amplified residual cannot false-fire onset.
      expect(
        CaptureRoutePolicy.onsetPlaybackHardFloor(AudioRoute.speaker),
        greaterThan(50.0 * CaptureRoutePolicy.gainForProfile(
          CaptureProfile.speaker,
        )),
      );
    });
  });
}
