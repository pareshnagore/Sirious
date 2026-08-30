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

  group('CaptureRoutePolicy onset floors', () {
    test('speaker floors are the 30 Aug platform-AEC recalibration', () {
      expect(CaptureRoutePolicy.onsetHardFloor(AudioRoute.speaker), 60.0);
      expect(
        CaptureRoutePolicy.onsetPlaybackHardFloor(AudioRoute.speaker),
        110.0,
      );
    });

    test('earphone floors are the raw-mic calibration', () {
      expect(CaptureRoutePolicy.onsetHardFloor(AudioRoute.earphones), 250.0);
      expect(
        CaptureRoutePolicy.onsetPlaybackHardFloor(AudioRoute.earphones),
        450.0,
      );
    });

    test('platform-AEC capture is quieter → speaker floors BELOW raw-mic', () {
      expect(
        CaptureRoutePolicy.onsetHardFloor(AudioRoute.speaker),
        lessThan(CaptureRoutePolicy.onsetHardFloor(AudioRoute.earphones)),
      );
      expect(
        CaptureRoutePolicy.onsetPlaybackHardFloor(AudioRoute.speaker),
        lessThan(CaptureRoutePolicy.onsetPlaybackHardFloor(AudioRoute.earphones)),
      );
    });
  });
}
