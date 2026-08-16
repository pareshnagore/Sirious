import 'package:flutter/material.dart';

import 'ui/voice_session_screen.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
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
