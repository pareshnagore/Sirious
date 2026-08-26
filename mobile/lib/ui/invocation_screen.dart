import 'dart:async';

import 'package:flutter/material.dart';

import '../models/session_phase.dart';
import '../models/transcript_turn.dart';
import '../services/sirious_session_controller.dart';
import 'widgets/status_indicator.dart';

/// Phase 5 C2: the voice-answer surface for an ambient invocation.
///
/// The room transcript already contains the request ("Sirious, can you
/// answer that?") — this screen hot-starts the voice session with that text
/// (`invoke`) plus the recent room tail (`seed`), plays the spoken answer
/// through the existing playback path, then AUTO-RETURNS to ambient mode
/// once the answer has drained from the speaker.
///
/// The [controller] is INJECTED (owned by the ambient flow), so this screen
/// never disposes it — it only ends the session before popping.
class InvocationScreen extends StatefulWidget {
  const InvocationScreen({
    super.key,
    required this.controller,
    required this.seed,
    required this.invoke,
  });

  final SiriousSessionController controller;
  final String seed;
  final String invoke;

  @override
  State<InvocationScreen> createState() => _InvocationScreenState();
}

class _InvocationScreenState extends State<InvocationScreen> {
  late final SiriousSessionController _controller;
  String? _status;
  bool _autoReturning = false;
  Timer? _drainPoll;

  @override
  void initState() {
    super.initState();
    _controller = widget.controller;
    _controller.addListener(_onChanged);
    _start();
  }

  @override
  void dispose() {
    _controller.removeListener(_onChanged);
    _drainPoll?.cancel();
    // Best-effort: never leave a live voice session behind on back-nav.
    if (_controller.isSessionActive) {
      unawaited(_controller.endSession());
    }
    super.dispose();
  }

  Future<void> _start() async {
    setState(() => _status = 'Connecting Sirious…');
    try {
      await _controller.startSession(
        seed: widget.seed,
        invoke: widget.invoke,
        duckCapture: true, // table mode: don't let the speaker feed the mic
      );
    } catch (error) {
      if (!mounted) return;
      setState(() => _status = 'Could not start: $error');
      return;
    }
  }

  void _onChanged() {
    if (!mounted) return;

    final phase = _controller.phase;
    if (phase == SessionPhase.error || _controller.errorMessage != null) {
      setState(() => _status = _controller.errorMessage);
      return;
    }

    switch (phase) {
      case SessionPhase.connecting:
      case SessionPhase.reconnecting:
        _status = 'Connecting…';
        break;
      case SessionPhase.listening:
        _status = _answerDone
            ? 'Returning to ambient…'
            : 'Invoked — getting ready…';
        break;
      case SessionPhase.responding:
        _status = 'Thinking…';
        break;
      case SessionPhase.playing:
        _status = 'Answering…';
        break;
      case SessionPhase.interrupting:
        _status = 'Interrupted';
        break;
      case SessionPhase.ending:
      case SessionPhase.idle:
      case SessionPhase.error:
        break;
    }

    // First completed answer → begin auto-return once the speaker drains.
    if (!_answerDone && _controller.turns.isNotEmpty) {
      _answerDone = true;
      _status = 'Answer complete — returning to ambient…';
      _startDrainPoll();
    }
    setState(() {});
  }

  bool _answerDone = false;

  void _startDrainPoll() {
    _drainPoll?.cancel();
    // Poll the playback service: return once the answer has fully played out.
    // Conditions: the queue is empty AND no feed activity for a settle window
    // AND the controller has moved past playing (turn complete → listening).
    // Any new feed activity (e.g. a follow-up answer) re-arms the window.
    const settle = Duration(milliseconds: 2500);
    const poll = Duration(milliseconds: 400);
    var lastGen = _controller.audioPlayback.activityGeneration;
    var lastChange = DateTime.now();

    _drainPoll = Timer.periodic(poll, (timer) {
      if (!mounted || _autoReturning) {
        return;
      }
      final gen = _controller.audioPlayback.activityGeneration;
      final queueEmpty = _controller.audioPlayback.queueLength == 0;
      final phase = _controller.phase;
      if (gen != lastGen ||
          phase == SessionPhase.playing ||
          phase == SessionPhase.responding) {
        lastGen = gen;
        lastChange = DateTime.now();
      }
      final quietFor = DateTime.now().difference(lastChange);
      if (queueEmpty && quietFor >= settle && phase == SessionPhase.listening) {
        timer.cancel();
        _returnToAmbient();
      }
    });
  }

  Future<void> _returnToAmbient() async {
    if (_autoReturning) return;
    _autoReturning = true;
    setState(() => _status = 'Returning to ambient…');
    await _controller.endSession();
    if (mounted) Navigator.of(context).pop(true);
  }

  /// Manual stop: end the session and pop immediately (stays in ambient).
  Future<void> _finishNow() async {
    if (_autoReturning) return;
    _autoReturning = true;
    await _controller.endSession();
    if (mounted) Navigator.of(context).pop(true);
  }

  @override
  Widget build(BuildContext context) {
    final turns = _controller.turns;
    final currentAssistant = _controller.currentAssistantText.trim();
    return Scaffold(
      appBar: AppBar(
        title: const Text('Sirious · Answering'),
        centerTitle: true,
        automaticallyImplyLeading: false,
      ),
      body: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                children: [
                  StatusIndicator(
                    phase: _controller.phase,
                    sessionId: _controller.sessionId,
                  ),
                  if (_status != null)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: Text(
                        _status!,
                        style: Theme.of(context).textTheme.bodyMedium,
                        textAlign: TextAlign.center,
                      ),
                    ),
                ],
              ),
            ),
            Expanded(
              child: turns.isEmpty && currentAssistant.isEmpty
                  ? Center(
                      child: Text(
                        'Sirious is answering from the room transcript…',
                        style: Theme.of(context).textTheme.bodyMedium,
                        textAlign: TextAlign.center,
                      ),
                    )
                  : ListView(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 16,
                        vertical: 8,
                      ),
                      children: [
                        _Bubble(
                          speaker: 'Room',
                          text: widget.invoke,
                          highlighted: true,
                        ),
                        for (final TranscriptTurn turn in turns) ...[
                          if (turn.userText.isNotEmpty)
                            _Bubble(
                              speaker: 'You',
                              text: turn.userText,
                              highlighted: false,
                            ),
                          if (turn.assistantText.isNotEmpty)
                            _Bubble(
                              speaker: 'Sirious',
                              text: turn.assistantText,
                              highlighted: false,
                            ),
                        ],
                        if (currentAssistant.isNotEmpty)
                          _Bubble(
                            speaker: 'Sirious',
                            text: currentAssistant,
                            highlighted: false,
                          ),
                      ],
                    ),
            ),
            Padding(
              padding: const EdgeInsets.all(16),
              child: OutlinedButton.icon(
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size(double.infinity, 48),
                ),
                icon: const Icon(Icons.graphic_eq),
                label: const Text('Back to ambient mode'),
                onPressed: _finishNow,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble({
    required this.speaker,
    required this.text,
    required this.highlighted,
  });

  final String speaker;
  final String text;
  final bool highlighted;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: highlighted
              ? theme.colorScheme.primaryContainer
              : theme.colorScheme.surfaceContainerHighest,
          borderRadius: BorderRadius.circular(12),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              speaker,
              style: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.primary,
                fontWeight: FontWeight.bold,
              ),
            ),
            const SizedBox(height: 4),
            Text(text),
          ],
        ),
      ),
    );
  }
}
