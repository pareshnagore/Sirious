import 'dart:math' as math;

/// Step 4.5 (31 Aug): ADAPTIVE voice levels for the manual-VAD speaker path.
///
/// Fixed floors are room-calibrated by definition (31 Aug lesson): the fixed
/// 150 playback floor false-fired in a loud room (residual 84–168), and the
/// fixed 240 onset floor ate quiet speech (166–249). Both floors must ride
/// the MEASURED room instead. All numbers are POST-GAIN RMS (speaker path
/// gain 4.0x applied at the capture choke point).
///
/// Pure static math — no audio, no controller state — so the thresholds can
/// be unit-tested against the real measured bands.
abstract final class AdaptiveVadPolicy {
  /// Ambient noise floor estimated as the MINIMUM of a window of recent
  /// near-silence chunk RMS values (the controller's existing convention:
  /// min, not mean, so speech never inflates the baseline).
  ///
  /// Same formula for both states (listen + playback). It is safe in
  /// playback because admission is speech-excluding (chunk must sit below
  /// the route's idle hard floor / 2 to enter the window) and because the
  /// platform AEC keeps playback-phase ambient genuine — answer tails can
  /// only push it UP, never down.
  static double ambientFloor(List<double> recentRms) {
    var floor = double.infinity;
    for (final v in recentRms) {
      if (v < floor) {
        floor = v;
      }
    }
    return floor.isFinite ? floor : 0.0;
  }

  /// Listening onset threshold: [ambientFloor] × [onsetRiseFactor], clamped
  /// to an absolute silence-of-speech safety band.
  ///
  /// - 80 lower clamp: real rooms are quiet (ambient 5–22); a pure ×4 of
  ///   ambient 5 = 20 would open windows on breath/movement sounds.
  /// - 150 upper clamp: barge-in speech measured down to 166 — anything
  ///   higher re-creates the eaten-words failure.
  static const double onsetRiseFactor = 4.0;
  static const double onsetLowerClamp = 80.0;
  static const double onsetUpperClamp = 150.0;

  static double listeningOnset(double ambient) =>
      (ambient * onsetRiseFactor).clamp(onsetLowerClamp, onsetUpperClamp);

  /// Onset threshold DURING PLAYBACK: must clear the room's amplified
  /// residual (the far-end echo the platform AEC leaves behind), which rides
  /// the same ambient air the user speaks through.
  ///
  /// - [ambient] × [onsetRiseFactor]: the room part (same ratio as idle
  ///   onset — speech at speaker distance sits ~10x above ambient).
  /// - [playbackResidual] × 2.0: the echo part. Residual is measured
  ///   25th-percentile playback-phase chunk RMS (rolling ~1 s window),
  ///   persisted across turns so each answer is seeded with the last
  ///   answer's echo — the first ~1 s of a NEW answer otherwise rides the
  ///   room's fixed-150 vulnerability. Room residual band measured
  ///   84–168 loud vs <30 quiet; ×2 keeps quiet rooms at the clamp and
  ///   clears loud-room transients.
  /// - 150 absolute clamp (post-gain): never lower — the amplified-residual
  ///   band from the 30 Aug calibration must not onset-fire; never higher
  ///   than the quietest real barge-in measured (166).
  static const double playbackResidualRiseFactor = 2.0;
  static const double playbackLowerClamp = 150.0;

  static double playbackOnset(double ambient, double playbackResidual) {
    final roomTerm =
        math.min(ambient, playbackAmbientCeiling) * onsetRiseFactor;
    final echoTerm = playbackResidual * playbackResidualRiseFactor;
    return math.max(math.max(roomTerm, echoTerm), playbackLowerClamp);
  }

  /// In-window HOLD level (window-close hysteresis, 31 Aug 00:00 lesson):
  /// the close decision uses this, not the onset threshold — natural speech
  /// dips between syllables and word gaps sit below onset but must HOLD the
  /// window.
  ///
  /// Step 4.5: hold = the CURRENT onset decision − [holdMargin]. NOT a
  /// function of ambient: deriving it independently (ambient×6) let the
  /// playback residual ratchet ambient up until hold > onset — windows
  /// closed mid-speech (caught by unit tests against the 31 Aug bands
  /// before any device flash). Below onset in EVERY room by construction.
  static const double holdMargin = 20.0;

  static double holdLevel(double currentOnset) =>
      currentOnset - holdMargin;

  /// The ambient term used during PLAYBACK: the window measured in
  /// LISTENING, clamped at [playbackAmbientCeiling] (×4 = 176). A stale
  /// loud-room window feeding the next session's playback room term is
  /// bounded here; the residual term carries the real protection above it.
  static const double playbackAmbientCeiling = 44.0;

  /// Rolling residual accumulator over the last ~1 s of playback chunks
  /// (10 chunks at 100 ms). The controller feeds every playback-phase chunk
  /// RMS; [residual] returns the 25th percentile — bursts and near-silence
  /// tails do not move it, the sustained echo level does.
  static const int residualWindowChunks = 10;

  static double residual(List<double> window) {
    if (window.isEmpty) {
      return 0.0;
    }
    final sorted = List<double>.of(window)..sort();
    final rank = (sorted.length * 0.25).ceil() - 1;
    return sorted[rank.clamp(0, sorted.length - 1)];
  }
}
