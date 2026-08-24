import 'package:flutter/material.dart';

import 'services/push_service.dart';
import 'ui/voice_session_screen.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  // FCM (Phase 4 chunk 4): fire-and-forget — push setup must never delay or
  // block the voice UI. Uncaught errors are contained inside PushService.
  PushService.init();
  runApp(const SiriousApp());
}

class SiriousApp extends StatelessWidget {
  const SiriousApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Sirious',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: Colors.indigo),
        useMaterial3: true,
      ),
      home: const VoiceSessionScreen(),
    );
  }
}
