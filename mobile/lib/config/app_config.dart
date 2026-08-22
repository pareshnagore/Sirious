/// Runtime configuration for the Sirious mobile client.
class AppConfig {
  AppConfig._();

  /// WebSocket endpoint. Override at build/run time:
  /// `flutter run --dart-define=WS_URL=wss://...`
  static const String wsUrl = String.fromEnvironment(
    'WS_URL',
    defaultValue:
        'wss://sirious-api-635321277027.asia-south1.run.app/ws',
  );

  /// REST base for the history API, derived from [wsUrl]:
  /// wss://host/ws → https://host
  static String get apiBase {
    final uri = Uri.parse(wsUrl);
    final scheme = uri.scheme == 'wss' ? 'https' : 'http';
    final port = uri.hasPort ? ':${uri.port}' : '';
    return '$scheme://${uri.host}$port';
  }

  /// Microphone PCM sent to server (protocol v1).
  static const int inputSampleRate = 16000;
  static const int inputChannels = 1;
  static const int inputChunkMs = 100;

  /// Assistant PCM received from server (protocol v1).
  static const int outputSampleRate = 24000;
  static const int outputChannels = 1;

  static const String protocolVersion = '1';
}
