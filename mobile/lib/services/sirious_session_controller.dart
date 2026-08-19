import 'dart:async';
import 'dart:io';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';

import '../models/latency_metrics.dart';
import '../models/session_phase.dart';
import '../models/transcript_turn.dart';
import 'audio_capture_service.dart';
import 'audio_playback_service.dart';
import 'websocket_client.dart';

/// Orchestrates WebSocket, mic capture, playback, and UI state.
class SiriousSessionController extends ChangeNotifier {
  SiriousSessionController() {
    _audioCapture = AudioCaptureService(onChunk: _onMicChunk);
    _webSocketClient = WebSocketClient(
      onJsonEvent: _onJsonEvent,
      onBinaryData: _onBinaryData,
      onDone: _onSocketDone,
      onError: _onSocketError,
    );
  }

  late final AudioCaptureService _audioCapture;
  final AudioPlaybackService _audioPlayback = AudioPlaybackService();
  late final WebSocketClient _webSocketClient;

  SessionPhase _phase = SessionPhase.idle;
  String? _sessionId;
  String? _errorMessage;
  String _currentUserText = '';
  String _currentAssistantText = '';
  final List<TranscriptTurn> _turns = [];
  final LatencyMetrics latency = LatencyMetrics();
  DateTime? _currentTurnStartedAt;

  SessionPhase get phase => _phase;
  String? get sessionId => _sessionId;
  String? get errorMessage => _errorMessage;
  String get currentUserText => _currentUserText;
  String get currentAssistantText => _currentAssistantText;
  List<TranscriptTurn> get turns => List.unmodifiable(_turns);
  bool get isSessionActive => _phase.isActive;

  /// Persistent, on-screen barge-in measurement log (newest first). Rendered
  /// directly in the UI so results are visible without files/logcat/screenshots.
  final List<String> _bargeInLog = <String>[];
  List<String> get bargeInLog => List.unmodifiable(_bargeInLog);

  // ── Network-blip resilience ─────────────────────────────────────────────
  // On a brief disconnect or a stalled socket we auto-reconnect to a FRESH
  // server+Gemini session (protocol v1 has no automated resumption). Any
  // on-device transcript already committed is preserved; the mid-utterance
  // partial turn is committed before reconnect so nothing on screen is lost.
  bool _allowReconnect = false; // true only while the user wants a live session
  int _reconnectAttempts = 0;
  static const int _maxReconnectAttempts = 5;
  static const int _keepaliveIntervalMs = 5000; // ping cadence
  static const int _stallTimeoutMs = 15000; // no server data → treat as dead
  Timer? _reconnectTimer;
  Timer? _keepaliveTimer;

  /// Backoff delay for the Nth retry (1s, 2s, 4s, … capped at 8s).
  Duration _reconnectDelay() {
    final exp = math.min(_reconnectAttempts, 3); // 2^3 = 8s cap
    return Duration(milliseconds: 1000 * (1 << exp));
  }

  static String _clock(DateTime dt) {
    String p(int v) => v.toString().padLeft(2, '0');
    final ms = dt.millisecond.toString().padLeft(3, '0');
    return '${p(dt.hour)}:${p(dt.minute)}:${p(dt.second)}.$ms';
  }

  /// Barge-in onset is detected relative to the ambient noise floor so it
  /// adapts to the device's real mic gain (AEC/AGC are device-dependent).
  /// Onset fires when: rms > max(noiseFloor * riseFactor, hardFloor).
  /// Speck chunks far above silence are excluded from the floor so speech
  /// never inflates the baseline (which would suppress detection).
  static const double _onsetHardFloor = 250.0;
  static const double _onsetRiseFactor = 4.0;
  static const int _noiseWindowChunks = 20; // ~2s at 100ms chunks

  final List<double> _recentRms = <double>[];
  double _onsetNoiseFloor = 0;
  double _windowPeakRms = 0; // loudest chunk in the CURRENT playback window
  bool _interruptionHandled = false; // true once this turn's barge-in ran

  double _rms(Uint8List pcm) {
    final sampleCount = pcm.length ~/ 2;
    if (sampleCount <= 0) {
      return 0;
    }
    final bd = ByteData.sublistView(pcm);
    var sumSq = 0.0;
    for (var i = 0; i < sampleCount; i++) {
      final s = bd.getInt16(i * 2, Endian.little).toDouble();
      sumSq += s * s;
    }
    return math.sqrt(sumSq / sampleCount);
  }

  /// Durable, pullable log on the device (read via `adb shell run-as
  /// com.sirious.sirious cat files/app_flutter/sirious_bargein.log` for a
  /// debug build). The on-screen summary only shows the LAST measurement
  /// briefly, which is unreliable to capture — this file is the source of
  /// truth for barge-in. `getApplicationDocumentsDirectory()` is the canonical
  /// writable app dir (Directory.systemTemp is not writable on Android).
  Future<void> _logToFile(String message) async {
    try {
      final dir = await getApplicationDocumentsDirectory();
      final file = File('${dir.path}/sirious_bargein.log');
      await file.writeAsString(
        '${DateTime.now().toIso8601String()} $message\n',
        mode: FileMode.append,
      );
    } catch (e) {
      debugPrint('bargein log write failed: $e');
    }
  }

  void _logBargeIn() {
    final onset = latency.bargeInOnsetAt;
    final interrupted = latency.lastInterruptedAt;
    final stopped = latency.bargeInAudioStoppedAt;

    // Copy live levels into the persisted display fields BEFORE any reset.
    latency.lastBargeInPeakRms = _windowPeakRms;
    latency.lastBargeInNoiseFloor = _onsetNoiseFloor;

    if (onset != null && interrupted != null && stopped != null) {
      final serverMs = interrupted.difference(onset).inMilliseconds;
      final flushMs = stopped.difference(interrupted).inMilliseconds;
      final totalMs = stopped.difference(onset).inMilliseconds;
      latency.lastBargeInServerMs = serverMs;
      latency.lastBargeInFlushMs = flushMs;
      latency.lastBargeInTotalMs = totalMs;
      latency.lastBargeInOnsetMissing = false;
      debugPrint(
        'BARGE_IN onset→interrupt(server+net)=${serverMs}ms · '
        'interrupt→stop(flush)=${flushMs}ms · TOTAL=${totalMs}ms',
      );
      unawaited(
        _logToFile(
          'BARGE_IN ok onset_ms=$serverMs flush_ms=$flushMs total_ms=$totalMs '
          'peak_rms=${_windowPeakRms.toStringAsFixed(0)} '
          'floor_rms=${_onsetNoiseFloor.toStringAsFixed(0)}',
        ),
      );
      _pushBargeInLine(
        '${_clock(onset)} → ${_clock(stopped)} · $totalMs ms '
        '(server $serverMs + flush $flushMs)',
      );
    } else {
      latency.lastBargeInTotalMs = null;
      latency.lastBargeInOnsetMissing = true;
      debugPrint(
        'BARGE_IN interrupted; onset missing '
        '(peakRms=${latency.lastBargeInPeakRms.toStringAsFixed(0)})',
      );
      unawaited(
        _logToFile(
          'BARGE_IN ONSET_MISSING peak_rms=${_windowPeakRms.toStringAsFixed(0)} '
          'floor_rms=${_onsetNoiseFloor.toStringAsFixed(0)} '
          'onset=$onset interrupted=$interrupted stopped=$stopped',
        ),
      );
      _pushBargeInLine(
        '${_clock(DateTime.now())} · onset missed '
        '(peak ${_windowPeakRms.toStringAsFixed(0)} '
        'floor ${_onsetNoiseFloor.toStringAsFixed(0)})',
      );
    }
    notifyListeners();
  }

  void _pushBargeInLine(String line) {
    _bargeInLog.insert(0, line);
    if (_bargeInLog.length > 12) {
      _bargeInLog.removeLast();
    }
  }

  /// Single code path for a barge-in, triggered either by Gemini's explicit
  /// `interrupted` event OR by a `user_transcript` arriving during playback.
  /// Runs at most once per turn (guarded by [_interruptionHandled]).
  Future<void> _handleInterruption(DateTime receivedAt) async {
    if (_interruptionHandled) {
      return;
    }
    _interruptionHandled = true;

    latency.lastInterruptedAt = receivedAt;
    _setPhase(SessionPhase.interrupting);

    DateTime? stoppedAt;
    try {
      stoppedAt = await _audioPlayback.flush();
    } catch (error, stackTrace) {
      debugPrint('Barge-in flush failed: $error\n$stackTrace');
    }
    latency.bargeInAudioStoppedAt = stoppedAt ?? DateTime.now();

    _logBargeIn();
    _endOnsetWindow();
    _commitCurrentTurn(interrupted: true);
    _setPhase(SessionPhase.listening);
    latency.resetForTurn();
  }

  Future<void> startSession() async {
    if (_phase.isActive) {
      return;
    }

    _errorMessage = null;
    _sessionId = null;
    _turns.clear();
    _currentUserText = '';
    _currentAssistantText = '';
    _currentTurnStartedAt = null;
    latency.sessionConnectedAt = null;
    latency.firstMicChunkAt = null;
    latency.resetForTurn();
    _allowReconnect = true;
    _reconnectAttempts = 0;
    _setPhase(SessionPhase.connecting);

    try {
      await _audioPlayback.init();
      await _webSocketClient.connect();
      await _audioCapture.start();
      _startKeepalive();
      _setPhase(SessionPhase.listening);
    } catch (error) {
      _allowReconnect = false;
      _errorMessage = error.toString();
      _setPhase(SessionPhase.error);
      await _cleanupSession(endPhase: SessionPhase.error);
    }
  }

  Future<void> endSession() async {
    if (!_phase.isActive && _phase != SessionPhase.error) {
      return;
    }

    _setPhase(SessionPhase.ending);
    await _cleanupSession(endPhase: SessionPhase.idle);
  }

  Future<void> _cleanupSession({required SessionPhase endPhase}) async {
    _allowReconnect = false;
    _stopKeepalive();
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _commitCurrentTurn(interrupted: _phase == SessionPhase.interrupting);

    try {
      if (_webSocketClient.isConnected) {
        _webSocketClient.sendStop();
      }
    } catch (_) {
      // Best effort.
    }

    await _audioCapture.stop();
    await _webSocketClient.disconnect();
    // Keep the native audio engine warm across sessions (only clear its queues).
    // Calling dispose() here tears down playback, and the plugin does not reset
    // its _needsStart flag on re-setup — the next session would have no audio.
    await _audioPlayback.flush();

    _sessionId = null;
    _currentUserText = '';
    _currentAssistantText = '';
    _currentTurnStartedAt = null;
    _setPhase(endPhase);
  }

  void _onMicChunk(Uint8List chunk) {
    latency.firstMicChunkAt ??= DateTime.now();

    // Barge-in onset: track mic energy while Sirious is talking (including the
    // brief `interrupting` window so a fast server response can't suppress it).
    // A single chunk above the adaptive threshold marks the user's onset.
    if (_phase == SessionPhase.playing ||
        _phase == SessionPhase.responding ||
        _phase == SessionPhase.interrupting) {
      final rms = _rms(chunk);
      if (rms > _windowPeakRms) {
        _windowPeakRms = rms;
      }

      // Update the quiet baseline only with near-silence samples.
      if (rms < _onsetHardFloor * 2) {
        _recentRms.add(rms);
        if (_recentRms.length > _noiseWindowChunks) {
          _recentRms.removeAt(0);
        }
      }
      var floor = double.infinity;
      for (final v in _recentRms) {
        if (v < floor) {
          floor = v;
        }
      }
      _onsetNoiseFloor = floor.isFinite ? floor : _onsetHardFloor;
      latency.lastBargeInNoiseFloor = _onsetNoiseFloor;

      final threshold = math.max(
        _onsetNoiseFloor * _onsetRiseFactor,
        _onsetHardFloor,
      );
      if (latency.bargeInOnsetAt == null && rms > threshold) {
        latency.bargeInOnsetAt = DateTime.now();
        unawaited(
          _logToFile(
            'ONSET rms=${rms.toStringAsFixed(0)} '
            'thr=${threshold.toStringAsFixed(0)} floor=${_onsetNoiseFloor.toStringAsFixed(0)}',
          ),
        );
      }
    }

    if (_webSocketClient.isConnected) {
      _webSocketClient.sendAudio(chunk);
    }
  }

  void _onBinaryData(Uint8List chunk) {
    latency.firstAssistantAudioAt ??= DateTime.now();

    if (_phase == SessionPhase.listening || _phase == SessionPhase.responding) {
      _setPhase(SessionPhase.playing);
    }

    _audioPlayback.enqueue(chunk);
  }

  Future<void> _onJsonEvent(Map<String, dynamic> event) async {
    final type = event['type'] as String?;
    debugPrint('WS_EVENT: $type');
    unawaited(_logToFile('EVENT $type ${event['text'] ?? ''}'));

    switch (type) {
      case 'session_started':
        _sessionId = event['session_id'] as String?;
        latency.sessionConnectedAt = DateTime.now();
        if (_phase == SessionPhase.reconnecting) {
          // Reconnect succeeded → resume listening on the fresh session.
          final recovered = _reconnectAttempts;
          _reconnectAttempts = 0;
          _pushBargeInLine(
            'Reconnected after $recovered retr${recovered == 1 ? 'y' : 'ies'}',
          );
          unawaited(
            _logToFile(
              'RECONNECT ok attempts=$recovered new_session=$_sessionId',
            ),
          );
          _setPhase(SessionPhase.listening);
        }
        break;

      case 'user_transcript':
        final text = event['text'] as String? ?? '';
        if (text.isEmpty) {
          break;
        }

        latency.firstUserTranscriptAt ??= DateTime.now();
        _currentTurnStartedAt ??= DateTime.now();
        _currentUserText += text;

        if (_phase == SessionPhase.playing) {
          // User is talking while Sirious is playing — that is a barge-in.
          // Flush + measure immediately; don't wait for a separate
          // `interrupted` event (Gemini may not always emit one).
          await _handleInterruption(DateTime.now());
        } else if (_phase == SessionPhase.listening) {
          _setPhase(SessionPhase.responding);
        }

        notifyListeners();
        break;

      case 'assistant_transcript':
        final text = event['text'] as String? ?? '';
        if (text.isEmpty) {
          break;
        }

        latency.firstAssistantTranscriptAt ??= DateTime.now();
        _currentTurnStartedAt ??= DateTime.now();
        _currentAssistantText += text;

        if (_phase == SessionPhase.listening ||
            _phase == SessionPhase.responding) {
          _setPhase(SessionPhase.playing);
        }

        notifyListeners();
        break;

      case 'response_finished':
        if (_phase == SessionPhase.responding) {
          _setPhase(SessionPhase.playing);
        }
        break;

      case 'interrupted':
        await _handleInterruption(DateTime.now());
        break;

      case 'turn_complete':
        _endOnsetWindow();
        _commitCurrentTurn(interrupted: false);
        _setPhase(SessionPhase.listening);
        latency.resetForTurn();
        break;

      case 'session_warning':
        _errorMessage =
            'Session warning: ${event['code']} (${event['time_left']})';
        notifyListeners();
        break;

      case 'error':
        _errorMessage = '${event['code']}: ${event['message']}';
        _setPhase(SessionPhase.error);
        notifyListeners();
        break;

      default:
        break;
    }
  }

  void _onSocketDone() {
    if (_phase.isActive && _allowReconnect) {
      // Network blip (server closed / lost) → reconnect instead of ending.
      _scheduleReconnect();
    } else if (_phase.isActive) {
      _errorMessage ??= 'Connection closed';
      unawaited(_cleanupSession(endPhase: SessionPhase.idle));
    }
  }

  void _onSocketError(Object error) {
    if (_phase.isActive && _allowReconnect) {
      unawaited(_logToFile('RECONNECT socket_error $_errorMessage'));
      _scheduleReconnect();
      return;
    }
    _errorMessage = error.toString();
    _setPhase(SessionPhase.error);
    notifyListeners();
    unawaited(_cleanupSession(endPhase: SessionPhase.error));
  }

  // ── Network-blip reconnect ──────────────────────────────────────────────

  /// Commit the in-flight partial turn (so nothing on screen is lost), stop
  /// playback, then schedule the next connect attempt with backoff.
  void _scheduleReconnect() {
    if (!_allowReconnect) {
      return;
    }
    if (_reconnectAttempts >= _maxReconnectAttempts) {
      _errorMessage =
          'Connection lost. Could not reconnect after '
          '$_maxReconnectAttempts attempts.';
      _setPhase(SessionPhase.error);
      notifyListeners();
      unawaited(_cleanupSession(endPhase: SessionPhase.error));
      return;
    }

    _commitCurrentTurn(interrupted: false);
    _endOnsetWindow();
    try {
      _audioPlayback.flush();
    } catch (_) {}

    _setPhase(SessionPhase.reconnecting);
    _reconnectAttempts++;
    final delay = _reconnectDelay();
    unawaited(
      _logToFile(
        'RECONNECT schedule attempt=$_reconnectAttempts delay_ms=${delay.inMilliseconds}',
      ),
    );
    _pushBargeInLine('Blip — reconnecting (attempt $_reconnectAttempts)…');
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(delay, () {
      _reconnectTimer = null;
      unawaited(_tryReconnect());
    });
  }

  Future<void> _tryReconnect() async {
    try {
      await _webSocketClient.connect();
      // On success we stay in `reconnecting` until `session_started` flips us
      // to `listening`. If this attempt drops again, onDone schedules the next.
    } catch (error) {
      unawaited(_logToFile('RECONNECT attempt failed: $error'));
      _scheduleReconnect();
    }
  }

  // ── Keepalive / stall watchdog ──────────────────────────────────────────
  // A "blip" can also silently stall the socket (still connected, no data).
  // We ping periodically and salvage if the server stops sending entirely.

  void _startKeepalive() {
    _keepaliveTimer?.cancel();
    _keepaliveTimer = Timer.periodic(
      const Duration(milliseconds: _keepaliveIntervalMs),
      (_) => _keepaliveTick(),
    );
  }

  void _stopKeepalive() {
    _keepaliveTimer?.cancel();
    _keepaliveTimer = null;
  }

  void _keepaliveTick() {
    if (!_allowReconnect) {
      return;
    }
    if (_webSocketClient.isConnected) {
      try {
        _webSocketClient.sendPing();
      } catch (_) {}
    }

    final sinceData = DateTime.now().difference(
      _webSocketClient.lastReceivedAt,
    );
    final phase = _phase;
    final isLivePhase =
        phase == SessionPhase.listening ||
        phase == SessionPhase.responding ||
        phase == SessionPhase.playing;
    if (isLivePhase && sinceData.inMilliseconds > _stallTimeoutMs) {
      unawaited(
        _logToFile(
          'RECONNECT stall detected idle_ms=${sinceData.inMilliseconds}',
        ),
      );
      // Tear down the stale socket — its cancelled subscription won't fire
      // onDone, so schedule the reconnect explicitly.
      unawaited(_webSocketClient.disconnect());
      _scheduleReconnect();
    }
  }

  void _endOnsetWindow() {
    // Reset only the LIVE window tracking. Persisted display values
    // (lastBargeIn*) must survive so the summary keeps the last result.
    _recentRms.clear();
    _onsetNoiseFloor = 0;
    _windowPeakRms = 0;
    _interruptionHandled = false;
  }

  void _commitCurrentTurn({required bool interrupted}) {
    final user = _currentUserText.trim();
    final assistant = _currentAssistantText.trim();

    if (user.isEmpty && assistant.isEmpty) {
      _currentUserText = '';
      _currentAssistantText = '';
      _currentTurnStartedAt = null;
      return;
    }

    _turns.add(
      TranscriptTurn(
        userText: user,
        assistantText: assistant,
        interrupted: interrupted,
        startedAt: _currentTurnStartedAt ?? DateTime.now(),
        completedAt: DateTime.now(),
      ),
    );

    _currentUserText = '';
    _currentAssistantText = '';
    _currentTurnStartedAt = null;
    notifyListeners();
  }

  void _setPhase(SessionPhase next) {
    if (_phase == next) {
      return;
    }

    _phase = next;
    notifyListeners();
  }

  @override
  void dispose() {
    unawaited(_cleanupSession(endPhase: SessionPhase.idle));
    unawaited(_audioCapture.dispose());
    super.dispose();
  }
}
