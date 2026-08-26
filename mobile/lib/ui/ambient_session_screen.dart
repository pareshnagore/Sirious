import 'package:flutter/material.dart';

import '../services/ambient_audio_bridge.dart';
import '../services/ambient_consent.dart';
import '../services/ambient_session_controller.dart';
import '../services/sirious_session_controller.dart';

/// Phase 5 C1: ambient listening screen. Phone on the table; live diarized
/// transcript; persistent recording indicator; Sirious is structurally
/// silent (no Gemini session exists in this mode).
class AmbientSessionScreen extends StatefulWidget {
  const AmbientSessionScreen({super.key, required this.voiceController});

  final SiriousSessionController voiceController;

  @override
  State<AmbientSessionScreen> createState() => _AmbientSessionScreenState();
}

class _AmbientSessionScreenState extends State<AmbientSessionScreen> {
  late final AmbientSessionController _controller;
  AmbientAudioBridge? _bridge;

  @override
  void initState() {
    super.initState();
    _controller = AmbientSessionController();
    _controller.addListener(_onChanged);
  }

  void _onChanged() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    _controller.removeListener(_onChanged);
    _bridge?.stop();
    _controller.dispose();
    super.dispose();
  }

  Future<void> _toggle() async {
    if (_controller.isActive) {
      await _bridge?.stop();
      _bridge = null;
    } else {
      // Block while a voice session is live — two mic clients can't share.
      if (widget.voiceController.isSessionActive) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
              content: Text('End the voice session before starting ambient mode')),
        );
        return;
      }
      if (!await ensureAmbientConsent(context)) return;
      final bridge = AmbientAudioBridge(controller: _controller);
      await bridge.start();
      _bridge = bridge;
    }
    if (mounted) setState(() {});
  }

  String _statusLabel() {
    switch (_controller.phase) {
      case AmbientPhase.idle:
        return 'Idle';
      case AmbientPhase.connecting:
        return 'Connecting…';
      case AmbientPhase.listening:
        return 'Listening to the room';
      case AmbientPhase.error:
        return 'Error';
    }
  }

  Color _statusColor(BuildContext context) {
    switch (_controller.phase) {
      case AmbientPhase.idle:
        return Theme.of(context).disabledColor;
      case AmbientPhase.connecting:
        return Colors.orange;
      case AmbientPhase.listening:
        return Colors.red;
      case AmbientPhase.error:
        return Theme.of(context).colorScheme.error;
    }
  }

  @override
  Widget build(BuildContext context) {
    final segments = _controller.segments;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Sirious · Ambient'),
        centerTitle: true,
      ),
      body: SafeArea(
        child: Column(
        children: [
          const SizedBox(height: 8),
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              // Recording indicator — required for the whole session.
              Icon(
                _controller.phase == AmbientPhase.listening
                    ? Icons.mic
                    : Icons.mic_off,
                color: _statusColor(context),
                size: 20,
              ),
              const SizedBox(width: 8),
              Text(
                _statusLabel(),
                style: Theme.of(context).textTheme.titleMedium,
              ),
            ],
          ),
          if (_controller.errorMessage != null)
            Padding(
              padding: const EdgeInsets.all(8),
              child: Text(
                _controller.errorMessage!,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
                textAlign: TextAlign.center,
              ),
            ),
          const SizedBox(height: 4),
          Expanded(
            child: segments.isEmpty
                ? Center(
                    child: Text(
                      _controller.phase == AmbientPhase.listening
                          ? 'Listening… speak near the phone'
                          : 'Start ambient mode to transcribe the room',
                      style: Theme.of(context).textTheme.bodyMedium,
                      textAlign: TextAlign.center,
                    ),
                  )
                : ListView.builder(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 16, vertical: 8),
                    itemCount: segments.length,
                    itemBuilder: (context, i) {
                      final seg = segments[i];
                      return Padding(
                        padding: const EdgeInsets.only(bottom: 10),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              'S${seg.speaker}',
                              style: Theme.of(context)
                                  .textTheme
                                  .labelSmall
                                  ?.copyWith(
                                    color: Theme.of(context)
                                        .colorScheme
                                        .primary,
                                    fontWeight: FontWeight.bold,
                                  ),
                            ),
                            Text(seg.text),
                          ],
                        ),
                      );
                    },
                  ),
          ),
          Padding(
            padding: const EdgeInsets.all(16),
            child: FilledButton.icon(
              style: FilledButton.styleFrom(
                minimumSize: const Size(double.infinity, 52),
                backgroundColor: _controller.isActive
                    ? Theme.of(context).colorScheme.error
                    : null,
              ),
              icon: Icon(_controller.isActive ? Icons.stop : Icons.graphic_eq),
              label: Text(_controller.isActive
                  ? 'Stop ambient'
                  : 'Start ambient'),
              onPressed: _toggle,
            ),
          ),
        ],
        ),
      ),
    );
  }
}
