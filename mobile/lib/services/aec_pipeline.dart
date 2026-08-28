import 'dart:math' as math;
import 'dart:typed_data';

import 'echo_canceller.dart';

/// Phase 6 Stage A: software AEC pipeline — MEASUREMENT mode.
///
/// Session policy (decided after iters 4-6): while the far end plays, the
/// controller HARD-DUCKS capture (no mic to Gemini, no barge-in onset) so a
/// weak-AEC echo can NEVER trigger self-interruption loops. This class then
/// has exactly one job: measure how much AEC3 cancels on this device.
///
/// Render design (what the data taught us):
///   - Feed the AEC at the playback tap (pre-feed), 10 ms frames.
///   - Silence-fill between answers so the far-end timeline stays continuous
///     (bursty render lost estimator lock: est 88ms → 0ms, 16.7 → 1.9 dB).
///   - The silence-fill runs on the CAPTURE clock (one render frame per
///     capture frame) — sample-exact, no wall-clock timers.
///   - Constant delay report (100 ms). AEC3 owns jitter internally.
class AecPipeline {
  AecPipeline({void Function(String line)? onStatsLine})
      : _onStatsLine = onStatsLine {
    _aec = EchoCanceller();
  }

  final void Function(String line)? _onStatsLine;

  late final EchoCanceller _aec;
  bool _disposed = false;

  // ── Render line ─────────────────────────────────────────────────────────
  // FIFO advanced by the capture clock; keeps last-frame alignment trivial
  // and the timeline continuous between answers.
  static const int _lineCapacityFrames = 10; // 100 ms
  final List<Int16List> _renderLine = <Int16List>[];
  bool _farEndActive = false;

  // ── Capture buffering ─────────────────────────────────────────────────────
  final List<int> _captureBuf = <int>[];

  // ── Metrics ───────────────────────────────────────────────────────────────
  double _preEnergySum = 0;
  double _postEnergySum = 0;
  int _metricFrames = 0;
  int _delayValidFrames = 0;
  int _delayValidPolls = 0;
  DateTime _lastStatsAt = DateTime.now();

  /// Set per capture frame: does the render reference CONTAIN real audio at
  /// this instant (echo possible now)? Without this gate the reduction
  /// window dilutes echo frames with silence frames (room tone has pre≈post
  /// energy → ratio→1 → ~0 dB even when cancellation is excellent).
  bool _refAudioThisFrame = false;

  static const int _constantDelayMs = 100;

  double get echoReductionDb {
    if (_metricFrames == 0) {
      return 0;
    }
    final pre = _preEnergySum / _metricFrames;
    final post = _postEnergySum / _metricFrames;
    if (pre <= 0) {
      return 0;
    }
    return 10 * math.log(pre / math.max(post, 1e-9)) / math.ln10;
  }

  double get delayValidRatio =>
      _delayValidPolls == 0 ? 0 : _delayValidFrames / _delayValidPolls;

  bool get delayValid => _aec.delayValid();

  /// True while the far end is producing audio (answer in flight).
  bool get isFarEndActive => _farEndActive;

  /// Called by the playback service pre-feed tap. Enqueues into the line.
  void feedRender(Uint8List pcm) {
    if (_disposed || pcm.isEmpty) {
      return;
    }
    _farEndActive = true;

    const frameBytes = 480; // 240 samples * 2
    final buf = Uint8List.fromList(pcm);
    for (var off = 0; off + frameBytes <= buf.length; off += frameBytes) {
      final frame = Int16List(240);
      final bd = ByteData.sublistView(buf, off, off + frameBytes);
      for (var i = 0; i < 240; i++) {
        frame[i] = bd.getInt16(i * 2, Endian.little);
      }
      _renderLine.add(frame);
    }
    while (_renderLine.length > _lineCapacityFrames) {
      _renderLine.removeAt(0);
    }
  }

  /// Capture leg: 16 kHz mic chunks in, echo-cancelled audio out. Also the
  /// single clock: every 10 ms capture frame advances the render line once.
  Uint8List processCaptureChunk(Uint8List chunk) {
    if (_disposed || chunk.isEmpty) {
      return chunk;
    }
    _captureBuf.addAll(chunk);

    const frameBytes = 320; // 160 samples * 2
    if (_captureBuf.length < frameBytes) {
      return Uint8List(0);
    }

    final wholeFrames = _captureBuf.length ~/ frameBytes;
    final bytes =
        Uint8List.fromList(_captureBuf.sublist(0, wholeFrames * frameBytes));
    final out = BytesBuilder(copy: false);
    final inFrame = Int16List(160);
    final outFrame = Int16List(160);

    for (var f = 0; f < wholeFrames; f++) {
      // Does the frame about to enter the AEC carry real far-end audio?
      final ref = _renderLine.isEmpty ? null : _renderLine.first;
      _refAudioThisFrame = ref != null && !_isSilent(ref);

      _advanceRenderLine();

      final start = f * frameBytes;
      final bd = ByteData.sublistView(bytes, start, start + frameBytes);
      for (var i = 0; i < 160; i++) {
        inFrame[i] = bd.getInt16(i * 2, Endian.little);
      }
      _aec.processCapture(inFrame, outFrame, _constantDelayMs);

      if (_refAudioThisFrame) {
        var pre = 0.0;
        var post = 0.0;
        for (var i = 0; i < 160; i++) {
          pre += inFrame[i] * inFrame[i];
          post += outFrame[i] * outFrame[i];
        }
        _preEnergySum += pre;
        _postEnergySum += post;
        _metricFrames++;
        _delayValidPolls++;
        if (_aec.delayValid()) {
          _delayValidFrames++;
        }
      }

      final obd = ByteData(frameBytes);
      for (var i = 0; i < 160; i++) {
        obd.setInt16(i * 2, outFrame[i], Endian.little);
      }
      out.add(obd.buffer.asUint8List());
    }

    _captureBuf.removeRange(0, wholeFrames * frameBytes);
    _maybeEmitStats();
    return out.toBytes();
  }

  void _advanceRenderLine() {
    if (_renderLine.isEmpty) {
      _aec.processRender(Int16List(240));
      return;
    }
    final head = _renderLine.removeAt(0);
    _aec.processRender(head);
    if (_renderLine.length < _lineCapacityFrames) {
      _renderLine.add(Int16List(240));
    }
  }

  static bool _isSilent(Int16List f) {
    for (var i = 0; i < f.length; i += 4) {
      if (f[i] != 0) {
        return false;
      }
    }
    return true;
  }

  void _maybeEmitStats() {
    if (_onStatsLine == null || _metricFrames < 20) {
      return;
    }
    final now = DateTime.now();
    if (now.difference(_lastStatsAt).inMilliseconds < 2000) {
      return;
    }
    _lastStatsAt = now;
    _onStatsLine(
      'AEC reduction=${echoReductionDb.toStringAsFixed(1)}dB '
      'delayValid=${(delayValidRatio * 100).toStringAsFixed(0)}% '
      'est=${_aec.delayEstimateMs()}ms '
      'frames=$_metricFrames',
    );
  }

  void resetMetrics() {
    _preEnergySum = 0;
    _postEnergySum = 0;
    _metricFrames = 0;
    _delayValidFrames = 0;
    _delayValidPolls = 0;
  }

  void dispose() {
    if (_disposed) {
      return;
    }
    _disposed = true;
    _aec.dispose();
  }
}
