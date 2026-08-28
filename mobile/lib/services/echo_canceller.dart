import 'dart:ffi';
import 'dart:typed_data';

import 'package:ffi/ffi.dart';

/// dart:ffi bindings for the native AEC3 wrapper (libsirious_aec.so).
///
/// Frame contract mirrors the C ABI: 10 ms interleaved int16 frames.
///   - render  = playback (far end), 24 kHz (240 samples/frame)
///   - capture = mic (near end), 16 kHz (160 samples/frame)
///
/// Phase 6 Stage A spike. Kill-switch metric: [delayValid] — if the AEC3
/// delay estimator never acquires a delay on the SM-E346B, latency tracking
/// has diverged and we fall back (hard-duck) per the Stage A plan.
///
/// The native side consumes frames synchronously within each call, so
/// preallocated in/out buffers are reused across calls (no per-call malloc).

// Native lib is bundled in the APK's jniLibs via the Gradle externalNativeBuild.
final DynamicLibrary _lib = () {
  try {
    return DynamicLibrary.open('libsirious_aec.so');
  } catch (_) {
    // Non-arm64 ABIs ship a stub .so; symbols resolve but calls return -1.
    // If even that fails, surface at create() time.
    return DynamicLibrary.process();
  }
}();

typedef _CreateNative = Pointer<Void> Function();
typedef _CreateDart = Pointer<Void> Function();

typedef _DestroyNative = Void Function(Pointer<Void>);
typedef _DestroyDart = void Function(Pointer<Void>);

typedef _ProcessRenderNative = Int32 Function(
    Pointer<Void>, Pointer<Int16>, Int32);
typedef _ProcessRenderDart = int Function(
    Pointer<Void>, Pointer<Int16>, int);

typedef _ProcessCaptureNative = Int32 Function(Pointer<Void>, Pointer<Int16>,
    Int32, Int32, Pointer<Int16>);
typedef _ProcessCaptureDart = int Function(
    Pointer<Void>, Pointer<Int16>, int, int, Pointer<Int16>);

typedef _DelayValidNative = Int32 Function(Pointer<Void>);
typedef _DelayValidDart = int Function(Pointer<Void>);

typedef _DelayEstimateNative = Int32 Function(Pointer<Void>);
typedef _DelayEstimateDart = int Function(Pointer<Void>);

final _create =
    _lib.lookupFunction<_CreateNative, _CreateDart>('sirious_aec_create');
final _destroy =
    _lib.lookupFunction<_DestroyNative, _DestroyDart>('sirious_aec_destroy');
final _processRender = _lib.lookupFunction<_ProcessRenderNative, _ProcessRenderDart>(
    'sirious_aec_process_render');
final _processCapture =
    _lib.lookupFunction<_ProcessCaptureNative, _ProcessCaptureDart>(
        'sirious_aec_process_capture');
final _delayValid =
    _lib.lookupFunction<_DelayValidNative, _DelayValidDart>('sirious_aec_delay_valid');
final _delayEstimate =
    _lib.lookupFunction<_DelayEstimateNative, _DelayEstimateDart>('sirious_aec_delay_estimate');

/// Echo canceller session. Create once per session; [dispose] at teardown.
///
/// Thread model: [processRender] is called from the playback path and
/// [processCapture] from the capture path — the native side serializes both
/// behind a mutex; on this app both calls originate from the main isolate.
class EchoCanceller {
  EchoCanceller() {
    _in = malloc<Int16>(_maxSamples);
    _out = malloc<Int16>(_maxSamples);
    _handle = _create();
    if (_handle == nullptr) {
      throw StateError('sirious_aec_create() returned null');
    }
  }

  static const int _maxSamples = 480; // 10 ms @ 24 kHz, the larger leg

  Pointer<Void> _handle = nullptr;
  late Pointer<Int16> _in;
  late Pointer<Int16> _out;
  bool _disposed = false;

  bool get isReady => !_disposed && _handle != nullptr;

  /// Feed 10 ms of playback audio (interleaved s16, 24 kHz, mono = 240
  /// samples). Returns 0 on success.
  int processRender(Int16List frame) {
    if (_disposed || frame.isEmpty || frame.length > _maxSamples) {
      return -1;
    }
    _in.asTypedList(frame.length).setAll(0, frame);
    return _processRender(_handle, _in, frame.length);
  }

  /// Feed 10 ms of mic audio (interleaved s16, 16 kHz, mono = 160 samples)
  /// and write the echo-cancelled output into [out] (same length).
  /// [delayMs] = how much later the mic hears what the speaker played.
  /// Returns 0 on success (then [out] is filled), -1 on failure ([out]
  /// untouched).
  int processCapture(Int16List frame, Int16List out, int delayMs) {
    if (_disposed ||
        frame.isEmpty ||
        frame.length > _maxSamples ||
        out.length < frame.length) {
      return -1;
    }
    _in.asTypedList(frame.length).setAll(0, frame);
    final ret = _processCapture(_handle, _in, frame.length, delayMs, _out);
    if (ret == 0) {
      out.setAll(0, _out.asTypedList(frame.length));
    }
    return ret;
  }

  /// Kill-switch metric: true once the AEC delay estimator has a measurement.
  /// Sustained false during playback => latency tracking diverged.
  bool delayValid() {
    if (_disposed) {
      return false;
    }
    return _delayValid(_handle) == 1;
  }

  /// The AEC's current delay estimate in ms (-1 when none). Diagnostics.
  int delayEstimateMs() {
    if (_disposed) {
      return -1;
    }
    return _delayEstimate(_handle);
  }

  void dispose() {
    if (_disposed) {
      return;
    }
    _disposed = true;
    if (_handle != nullptr) {
      _destroy(_handle);
      _handle = nullptr;
    }
    calloc.free(_in);
    calloc.free(_out);
  }
}
