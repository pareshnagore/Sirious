import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// One-time consent gate for ambient (group-conversation) listening.
/// Records acceptance locally; the recording indicator is shown for the
/// whole duration of every ambient session regardless.
class AmbientConsent {
  AmbientConsent._();
  static const _key = 'ambient_consent_v1';

  static Future<bool> isAccepted() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getBool(_key) ?? false;
  }

  static Future<void> accept() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(_key, true);
  }
}

/// Returns true when ambient listening may start (already accepted, or the
/// user accepts now). Push [context] only from a mounted State.
Future<bool> ensureAmbientConsent(BuildContext context) async {
  if (await AmbientConsent.isAccepted()) return true;
  if (!context.mounted) return false;

  final accepted = await showDialog<bool>(
    context: context,
    barrierDismissible: false,
    builder: (context) => AlertDialog(
      icon: const Icon(Icons.graphic_eq),
      title: const Text('Listen to the room?'),
      content: const Text(
        'Ambient mode keeps the microphone open and transcribes the '
        'conversation around the phone, labeling speakers as S1, S2, …\n\n'
        '• Sirious stays completely silent until you invoke it\n'
        '• Audio is transcribed by a speech-to-text cloud service\n'
        '• A recording indicator stays visible the whole time\n'
        '• Everyone at the table should know the phone is listening\n\n'
        'Transcripts are saved to your History like normal sessions.',
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context, false),
          child: const Text('Not now'),
        ),
        FilledButton(
          onPressed: () => Navigator.pop(context, true),
          child: const Text('Enable ambient mode'),
        ),
      ],
    ),
  );
  if (accepted == true) {
    await AmbientConsent.accept();
  }
  return accepted == true;
}
