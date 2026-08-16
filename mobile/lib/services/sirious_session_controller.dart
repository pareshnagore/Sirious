import 'dart:async';

import 'package:flutter/foundation.dart';

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
    _setPhase(SessionPhase.connecting);

    try {
      await _audioPlayback.init();
      await _webSocketClient.connect();
      await _audioCapture.start();
      _setPhase(SessionPhase.listening);
    } catch (error) {
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

    if (_webSocketClient.isConnected) {
      _webSocketClient.sendAudio(chunk);
    }
  }

  void _onBinaryData(Uint8List chunk) {
    latency.firstAssistantAudioAt ??= DateTime.now();

    if (_phase == SessionPhase.listening ||
        _phase == SessionPhase.responding) {
      _setPhase(SessionPhase.playing);
    }

    _audioPlayback.enqueue(chunk);
  }

  void _onJsonEvent(Map<String, dynamic> event) {
    final type = event['type'] as String?;

    switch (type) {
      case 'session_started':
        _sessionId = event['session_id'] as String?;
        latency.sessionConnectedAt = DateTime.now();
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
          _setPhase(SessionPhase.interrupting);
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
        latency.lastInterruptedAt = DateTime.now();
        _setPhase(SessionPhase.interrupting);
        unawaited(_audioPlayback.flush());
        _commitCurrentTurn(interrupted: true);
        _setPhase(SessionPhase.listening);
        latency.resetForTurn();
        break;

      case 'turn_complete':
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
    if (_phase.isActive) {
      _errorMessage ??= 'Connection closed';
      unawaited(_cleanupSession(endPhase: SessionPhase.idle));
    }
  }

  void _onSocketError(Object error) {
    _errorMessage = error.toString();
    _setPhase(SessionPhase.error);
    notifyListeners();
    unawaited(_cleanupSession(endPhase: SessionPhase.error));
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
