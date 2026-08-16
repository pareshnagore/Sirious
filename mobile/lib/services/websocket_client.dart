import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:web_socket_channel/web_socket_channel.dart';

import '../config/app_config.dart';

typedef JsonEventHandler = void Function(Map<String, dynamic> event);
typedef BinaryHandler = void Function(Uint8List data);
typedef VoidCallback = void Function();

/// Thin WebSocket transport for protocol v1.
class WebSocketClient {
  WebSocketClient({
    required this.onJsonEvent,
    required this.onBinaryData,
    this.onDone,
    this.onError,
    String? wsUrl,
  }) : _wsUrl = wsUrl ?? AppConfig.wsUrl;

  final String _wsUrl;
  final JsonEventHandler onJsonEvent;
  final BinaryHandler onBinaryData;
  final VoidCallback? onDone;
  final void Function(Object error)? onError;

  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _subscription;

  bool get isConnected => _channel != null;

  Future<void> connect() async {
    await disconnect();

    final channel = WebSocketChannel.connect(Uri.parse(_wsUrl));
    _channel = channel;

    _subscription = channel.stream.listen(
      _handleMessage,
      onDone: () {
        onDone?.call();
        _channel = null;
      },
      onError: (Object error) {
        onError?.call(error);
      },
      cancelOnError: true,
    );
  }

  void _handleMessage(dynamic message) {
    if (message is List<int>) {
      onBinaryData(Uint8List.fromList(message));
      return;
    }

    if (message is Uint8List) {
      onBinaryData(message);
      return;
    }

    if (message is String) {
      try {
        final decoded = jsonDecode(message);

        if (decoded is Map<String, dynamic>) {
          onJsonEvent(decoded);
        }
      } on FormatException {
        // Ignore non-JSON text frames.
      }
    }
  }

  void sendAudio(Uint8List pcm) {
    _channel?.sink.add(pcm);
  }

  void sendStop() {
    _channel?.sink.add('stop');
  }

  void sendPing() {
    _channel?.sink.add('ping');
  }

  Future<void> disconnect() async {
    await _subscription?.cancel();
    _subscription = null;

    await _channel?.sink.close();
    _channel = null;
  }
}
