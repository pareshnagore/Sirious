import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../config/app_config.dart';
import 'auth_service.dart';

/// One diarized utterance received from the ambient endpoint.
@immutable
class AmbientSegment {
  const AmbientSegment({
    required this.speaker,
    required this.text,
    required this.startS,
    required this.endS,
  });

  final int speaker;
  final String text;
  final double startS;
  final double endS;

  factory AmbientSegment.fromJson(Map<String, dynamic> j) => AmbientSegment(
        speaker: (j['speaker'] as num?)?.toInt() ?? 0,
        text: j['text'] as String? ?? '',
        startS: (j['start_s'] as num?)?.toDouble() ?? 0,
        endS: (j['end_s'] as num?)?.toDouble() ?? 0,
      );
}

enum AmbientPhase { idle, connecting, listening, error }

/// Phase 5 C1: live ambient session — mic → /ws/ambient → diarized segments.
///
/// Structural silence: this endpoint has NO Gemini session, nothing can talk.
/// Reconnects reuse the same client_session_id so the backend EXTENDS one
/// Firestore conversation doc across blips (ambient turns are append-only).
class AmbientSessionController extends ChangeNotifier {
  AmbientSessionController();

  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _subscription;
  final List<AmbientSegment> _segments = [];
  AmbientPhase _phase = AmbientPhase.idle;
  String? _errorMessage;
  String? _clientSessionId;
  bool _stopping = false;

  AmbientPhase get phase => _phase;
  String? get errorMessage => _errorMessage;
  List<AmbientSegment> get segments => List.unmodifiable(_segments);
  bool get isActive => _phase == AmbientPhase.connecting || _phase == AmbientPhase.listening;

  Future<void> start() async {
    if (isActive) return;
    _phase = AmbientPhase.connecting;
    _errorMessage = null;
    _segments.clear();
    _stopping = false;
    _clientSessionId ??=
        'amb-${DateTime.now().millisecondsSinceEpoch}-${DateTime.now().microsecond % 1000}';
    notifyListeners();

    final token = await AuthService().getToken();
    if (token == null || token.isEmpty) {
      _phase = AmbientPhase.error;
      _errorMessage = 'No API token set (History → 🔑)';
      notifyListeners();
      return;
    }

    final base = Uri.parse(AppConfig.wsUrl);
    final wsUri = base.replace(
      path: '/ws/ambient',
      queryParameters: {
        'token': token,
        'client_session_id': _clientSessionId!,
      },
    );

    try {
      _channel = WebSocketChannel.connect(wsUri);
      _subscription = _channel!.stream.listen(
        _onMessage,
        onDone: _onDone,
        onError: (Object e) => _onError(e),
      );
      // First server frame must be session_started; a handshake rejection
      // surfaces as onDone/onError shortly after connect.
      await _channel!.ready;
      _phase = AmbientPhase.listening;
      notifyListeners();
    } catch (e) {
      _phase = AmbientPhase.error;
      _errorMessage = 'Ambient connect failed: $e';
      notifyListeners();
    }
  }

  Future<void> stop() async {
    if (!isActive && _phase != AmbientPhase.listening) return;
    _stopping = true;
    _channel?.sink.add('stop');
    await Future<void>.delayed(const Duration(milliseconds: 150));
    await _teardown();
    _phase = AmbientPhase.idle;
    notifyListeners();
  }

  /// Raw PCM from the mic bridge → server as a binary frame.
  void sendAudio(List<int> pcm) {
    _channel?.sink.add(Uint8List.fromList(pcm));
  }

  void _onMessage(dynamic message) {
    if (message is! String) return;
    try {
      final decoded = jsonDecode(message);
      if (decoded is! Map<String, dynamic>) return;
      if (decoded['type'] == 'ambient_segment') {
        _segments.add(AmbientSegment.fromJson(decoded));
        notifyListeners();
      } else if (decoded['type'] == 'error') {
        _errorMessage = decoded['message'] as String? ?? 'server error';
        notifyListeners();
      }
    } on FormatException {
      // ignore non-JSON frames
    }
  }

  void _onDone() {
    if (_stopping) return;
    // Server closed us mid-session (deploy, crash, network). Surface as
    // error — ambient auto-reconnect is C5 hardening, not C1.
    _phase = AmbientPhase.error;
    _errorMessage ??= 'Ambient session ended unexpectedly';
    _teardown();
    notifyListeners();
  }

  void _onError(Object error) {
    if (_stopping) return;
    _phase = AmbientPhase.error;
    _errorMessage = '$error';
    _teardown();
    notifyListeners();
  }

  Future<void> _teardown() async {
    await _subscription?.cancel();
    _subscription = null;
    try {
      await _channel?.sink.close();
    } catch (_) {}
    _channel = null;
  }

  @override
  void dispose() {
    _stopping = true;
    _teardown();
    super.dispose();
  }
}
