import 'dart:ffi';
import 'dart:typed_data';

import 'package:ffi/ffi.dart';

/// dart:ffi bindings for the native AEC3 wrapper (libsirious_aec.so).
///
/// Frame contract mirrors the C ABI: 10 ms interleaved int16 frames.
///   - render  = playback (far end), 24 kHz
///   - capture = mic (near end), 16 kHz, with the A/V delay hint in ms
///
/// Phase 6 Stage A spike. Kill-switch metric: [delayValid] — if the AEC3
/// delay estimator never acquires a delay on the SM-E346B, latency tracking
/// has diverged and we fall back (hard-duck) per the Stage A plan.

// Native lib is bundled in the APK's jniLibs via the Gradle externalNativeBuild.
final DynamicLibrary _lib = () {
  try {
    return DynamicLibrary.open('libsirious_aec.so');
  } catch (e) {
    // Symbol-level failures surface later as clear errors; keep init resilient.
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

typedef _ProcessCaptureNative = Int32 Function(
    Pointer<Void>, Pointer<Int16>, Int32, Int32, Pointer<Int16>);
typedef _ProcessCaptureDart = int Function(
    Pointer<Void>, Pointer<Int16>, int, int, Pointer<Int16>);

typedef _DelayValidNative = Int32 Function(Pointer<Void>);
typedef _DelayValidDart = int Function(Pointer<Void>);

final _create = _lib.lookupFunction<_CreateNative, _CreateDart>('sirious_aec_create');
final _destroy =
    _lib.lookupFunction<_DestroyNative, _DestroyDart>('sirious_aec_destroy');
final _processRender = _lib
    .lookupFunction<_ProcessRenderNative, _ProcessRenderDart>('sirious_aec_process_render');
final _processCapture = _lib
    .lookupFunction<_ProcessCaptureNative, _ProcessCaptureDart>('sirious_aec_process_capture');
final _delayValid = _lib
    .lookupFunction<_DelayValidNative, _DelayValidDart>('sirious_aec_delay_valid');

/// Echo canceller session. Create once per session; [dispose] at teardown.
///
/// Thread model: [processRender] is called from the playback path and
/// [processCapture] from the capture path — both funnel into a native mutex,
/// but keep calls off the UI isolate as with the rest of the audio path.
class EchoCanceller {
  EchoCanceller() {
    _handle = _create();
    if (_handle == nullptr) {
      throw StateError('sirious_aec_create() returned null');
    }
  }

  Pointer<Void> _handle = nullptr;
  bool _disposed = false;

  bool get isReady => !_disposed && _handle != nullptr;

  /// Feed 10 ms of playback audio (interleaved s16, 24 kHz, mono).
  /// [samplesPerChannel] = 240 for a standard 10 ms frame.
  /// Returns 0 on success.
  int processRender(Int16List frame) {
    if (_disposed) return -1;
    final ptr = frame.isEmpty ? nullptr : _int16Ptr(frame);
    return _processRender(_handle, ptr, frame.length);
  }

  /// Feed 10 ms of mic audio (interleaved s16, 16 kHz, mono) and get the
  /// echo-cancelled output written into [out].
  /// [delayMs] = how much later the mic hears what the speaker played.
  /// Returns 0 on success.
  int processCapture(Int16List frame, Int16List out, int delayMs) {
    if (_disposed) return -1;
    return _processCapture(
        _handle, _int16Ptr(frame), frame.length, delayMs, _int16Ptr(out));
  }

  /// Kill-switch metric: true once the AEC delay estimator has a measurement.
  /// Sustained false during playback => latency tracking diverged.
  bool delayValid() {
    if (_disposed) return false;
    return _delayValid(_handle) == 1;
  }

  void dispose() {
    if (_disposed) return;
    _disposed = true;
    if (_handle != nullptr) {
      _destroy(_handle);
      _handle = nullptr;
    }
  }
}

/// Copies Dart data into freshly allocated native memory (freed on return —
/// the native side consumes it synchronously within the call).
Pointer<Int16> _int16Ptr(Int16List data) {
  final ptr = malloc<Int16>(data.length);
  ptr.asTypedList(data.length).setAll(0, data);
  return ptr;
}
