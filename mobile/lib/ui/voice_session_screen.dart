import 'package:flutter/material.dart';

import '../config/app_config.dart';
import '../models/session_phase.dart';
import '../services/sirious_session_controller.dart';
import 'widgets/status_indicator.dart';
import 'widgets/transcript_panel.dart';

class VoiceSessionScreen extends StatefulWidget {
  const VoiceSessionScreen({super.key});

  @override
  State<VoiceSessionScreen> createState() => _VoiceSessionScreenState();
}

class _VoiceSessionScreenState extends State<VoiceSessionScreen> {
  late final SiriousSessionController _controller;

  @override
  void initState() {
    super.initState();
    _controller = SiriousSessionController();
    _controller.addListener(_onControllerChanged);
  }

  void _onControllerChanged() {
    if (mounted) {
      setState(() {});
    }
  }

  @override
  void dispose() {
    _controller.removeListener(_onControllerChanged);
    _controller.dispose();
    super.dispose();
  }

  Future<void> _toggleSession() async {
    if (_controller.isSessionActive) {
      await _controller.endSession();
    } else {
      await _controller.startSession();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Sirious'), centerTitle: true),
      body: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.all(16),
              child: StatusIndicator(
                phase: _controller.phase,
                sessionId: _controller.sessionId,
              ),
            ),
            if (_controller.errorMessage != null)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: Text(
                  _controller.errorMessage!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ),
            if (_controller.phase == SessionPhase.reconnecting)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(8),
                  decoration: BoxDecoration(
                    color: Theme.of(
                      context,
                    ).colorScheme.surfaceContainerHighest,
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Text(
                    'Network blip — reconnecting automatically…',
                    style: Theme.of(context).textTheme.bodyMedium,
                  ),
                ),
              ),
            Expanded(
              child: TranscriptPanel(
                turns: _controller.turns,
                currentUserText: _controller.currentUserText,
                currentAssistantText: _controller.currentAssistantText,
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: Text(
                _controller.latency.summary,
                style: Theme.of(context).textTheme.bodySmall,
                textAlign: TextAlign.center,
              ),
            ),
            if (_controller.bargeInLog.isNotEmpty)
              Container(
                margin: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                padding: const EdgeInsets.all(8),
                constraints: const BoxConstraints(maxHeight: 210),
                decoration: BoxDecoration(
                  color: Theme.of(context).colorScheme.surfaceContainerHighest,
                  borderRadius: BorderRadius.circular(8),
                ),
                child: ListView.builder(
                  shrinkWrap: true,
                  itemCount: _controller.bargeInLog.length,
                  itemBuilder: (context, i) => Text(
                    _controller.bargeInLog[i],
                    style: Theme.of(
                      context,
                    ).textTheme.bodySmall?.copyWith(fontFamily: 'monospace'),
                  ),
                ),
              )
            else
              const Padding(
                padding: EdgeInsets.symmetric(horizontal: 16, vertical: 6),
                child: Text(
                  'Barge-in log: none yet — interrupt Sirious MID-sentence to record one.',
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 11),
                ),
              ),
            Padding(
              padding: const EdgeInsets.all(16),
              child: SizedBox(
                width: double.infinity,
                child: FilledButton.icon(
                  onPressed:
                      _controller.phase == SessionPhase.connecting ||
                          _controller.phase == SessionPhase.ending
                      ? null
                      : _toggleSession,
                  icon: Icon(
                    _controller.isSessionActive ? Icons.stop : Icons.mic,
                  ),
                  label: Text(
                    _controller.isSessionActive
                        ? 'End session'
                        : 'Start session',
                  ),
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Text(
                'Protocol v${AppConfig.protocolVersion} · ${AppConfig.wsUrl}',
                style: Theme.of(context).textTheme.bodySmall,
                textAlign: TextAlign.center,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
