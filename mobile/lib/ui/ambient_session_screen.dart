import 'dart:async';

import 'package:flutter/material.dart';

import '../models/session_phase.dart';
import '../models/transcript_turn.dart';
import '../services/ambient_audio_bridge.dart';
import '../services/ambient_consent.dart';
import '../services/ambient_session_controller.dart';
import '../services/sirious_session_controller.dart';

/// Phase 5 C1+C2+C: the one ambient screen. Phone on the table; live
/// diarized room transcript; persistent recording indicator; Sirious is
/// structurally silent until the C2 spotter hears "Sirious" — then the
/// voice session runs INLINE on this same screen (no screen swap): the
/// answer renders as an assistant bubble under the room transcript, the
/// mic is ducked while speaking, and ambient listening auto-resumes after
/// the answer drains. One screen, one session, one History entry.
class AmbientSessionScreen extends StatefulWidget {
  const AmbientSessionScreen({super.key, required this.voiceController});

  final SiriousSessionController voiceController;

  @override
  State<AmbientSessionScreen> createState() => _AmbientSessionScreenState();
}

class _AmbientSessionScreenState extends State<AmbientSessionScreen> {
  late final AmbientSessionController _controller;
  AmbientAudioBridge? _bridge;
  final ScrollController _scroll = ScrollController();

  /// True between spotting "Sirious" and the answer finishing — suppresses
  /// duplicate triggers and shows the invoking/answering banner.
  bool _invoking = false;
  String _invokeText = '';
  String? _invokeError;

  /// First committed voice turn marks the answer as done → start the
  /// playback-drain timer that returns to ambient.
  bool _answerDone = false;
  bool _autoReturning = false;
  Timer? _drainPoll;

  @override
  void initState() {
    super.initState();
    _controller = AmbientSessionController();
    _controller.addListener(_onChanged);
    _controller.onInvocation = _handleInvocation;
    widget.voiceController.addListener(_onVoiceChanged);
  }

  void _onChanged() {
    if (mounted) setState(() {});
    _maybeAutoScroll();
  }

  void _onVoiceChanged() {
    if (!mounted) return;
    setState(() {});
    // First completed answer → arm the auto-return drain poll.
    if (_invoking &&
        !_answerDone &&
        widget.voiceController.turns.isNotEmpty) {
      _answerDone = true;
      _startDrainPoll();
    }
    final err = widget.voiceController.errorMessage;
    if (_invoking && err != null && _invokeError == null) {
      _invokeError = err;
    }
    // A failed voice start (auth, model, network) must not leave the
    // invocation hanging — fall back to ambient automatically.
    if (_invoking &&
        !_autoReturning &&
        widget.voiceController.phase == SessionPhase.error) {
      unawaited(_resumeAmbient());
    }
  }

  @override
  void dispose() {
    _controller.onInvocation = null;
    _controller.removeListener(_onChanged);
    widget.voiceController.removeListener(_onVoiceChanged);
    _drainPoll?.cancel();
    _bridge?.stop();
    _controller.dispose();
    _scroll.dispose();
    super.dispose();
  }

  // ── Invocation (inline answer) ──────────────────────────────────────────

  /// "Sirious" spotted in the room transcript. Stop ambient capture, hot-start
  /// a voice session seeded with the room tail + the trigger text (reusing the
  /// ambient client_session_id so History stays ONE doc — Phase 5 C+B), render
  /// the answer inline, then resume ambient once the speaker drains.
  Future<void> _handleInvocation(
    AmbientSegment trigger,
    List<AmbientSegment> tail,
  ) async {
    if (_invoking ||
        widget.voiceController.isSessionActive ||
        !mounted) {
      return;
    }
    setState(() {
      _invoking = true;
      _invokeText = trigger.text;
      _invokeError = null;
      _answerDone = false;
      _autoReturning = false;
    });

    await _bridge?.stop();
    _bridge = null;
    if (!mounted) return;

    final seed = tail.map((s) => 'S${s.speaker}: ${s.text}').join('\n');
    try {
      await widget.voiceController.startSession(
        seed: seed,
        invoke: trigger.text,
        duckCapture: true, // table mode: never let the speaker feed the mic
        clientSessionId: _controller.clientSessionId,
      );
    } catch (error) {
      if (!mounted) return;
      _invokeError = 'Could not start: $error';
      await _resumeAmbient();
    }
  }

  /// Restart ambient capture WITHOUT wiping the visible room transcript.
  bool _resumingAmbient = false;
  Future<void> _resumeAmbient() async {
    if (_resumingAmbient) return;
    _resumingAmbient = true;
    _drainPoll?.cancel();
    _answerDone = false;
    _autoReturning = false;
    final bridge = AmbientAudioBridge(controller: _controller);
    await bridge.start(clearSegments: false);
    _bridge = _controller.phase == AmbientPhase.listening ? bridge : null;
    _resumingAmbient = false;
    if (mounted) setState(() => _invoking = false);
  }

  /// Poll the playback service: return to ambient once the answer has fully
  /// drained (queue empty + settle window + phase back to listening).
  void _startDrainPoll() {
    _drainPoll?.cancel();
    const settle = Duration(milliseconds: 2500);
    const poll = Duration(milliseconds: 400);
    var lastGen = widget.voiceController.audioPlayback.activityGeneration;
    var lastChange = DateTime.now();

    _drainPoll = Timer.periodic(poll, (timer) {
      if (!mounted || _autoReturning) {
        return;
      }
      final gen = widget.voiceController.audioPlayback.activityGeneration;
      final queueEmpty = widget.voiceController.audioPlayback.queueLength == 0;
      final phase = widget.voiceController.phase;
      if (gen != lastGen ||
          phase == SessionPhase.playing ||
          phase == SessionPhase.responding) {
        lastGen = gen;
        lastChange = DateTime.now();
      }
      final quietFor = DateTime.now().difference(lastChange);
      if (queueEmpty && quietFor >= settle && phase == SessionPhase.listening) {
        timer.cancel();
        unawaited(_finishAnswer());
      }
    });
  }

  Future<void> _finishAnswer() async {
    if (_autoReturning) return;
    _autoReturning = true;
    setState(() {});
    await widget.voiceController.endSession();
    await _resumeAmbient();
  }

  /// Manual stop: end the voice session now and drop back to ambient.
  Future<void> _finishNow() async {
    if (_autoReturning) return;
    _autoReturning = true;
    setState(() {});
    await widget.voiceController.endSession();
    await _resumeAmbient();
  }

  // ── Ambient toggle ──────────────────────────────────────────────────────

  Future<void> _toggle() async {
    if (_invoking) {
      await _finishNow();
      return;
    }
    if (_controller.isActive) {
      await _bridge?.stop();
      _bridge = null;
    } else {
      // Block while a voice session is live — two mic clients can't share.
      if (widget.voiceController.isSessionActive || _invoking) {
        if (!mounted) return;
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            content: Text('End the voice session before starting ambient mode'),
          ),
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

  // ── Status / rendering ──────────────────────────────────────────────────

  String _statusLabel() {
    if (_invoking) {
      if (_autoReturning) return 'Returning to ambient…';
      if (_answerDone) return 'Answer complete — returning to ambient…';
      return 'Invoking Sirious…';
    }
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
    if (_invoking) return Colors.deepPurple;
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

  Widget _segmentTile(AmbientSegment seg) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            'S${seg.speaker}',
            style: Theme.of(context).textTheme.labelSmall?.copyWith(
                  color: Theme.of(context).colorScheme.primary,
                  fontWeight: FontWeight.bold,
                ),
          ),
          Text(seg.text),
        ],
      ),
    );
  }

  /// Inline invocation widgets appended BELOW the room transcript while a
  /// voice answer is live or just finished (auto-return pending).
  List<Widget> _invocationExtras() {
    if (!_invoking) return const [];
    final out = <Widget>[];
    out.add(
      _Bubble(speaker: 'Room', text: _invokeText, highlighted: true),
    );
    for (final TranscriptTurn turn in widget.voiceController.turns) {
      if (turn.userText.isNotEmpty) {
        out.add(_Bubble(speaker: 'You', text: turn.userText));
      }
      if (turn.assistantText.isNotEmpty) {
        out.add(_Bubble(speaker: 'Sirious', text: turn.assistantText));
      }
    }
    final current = widget.voiceController.currentAssistantText.trim();
    if (current.isNotEmpty) {
      out.add(_Bubble(speaker: 'Sirious', text: current));
    }
    if (_invokeError != null) {
      out.add(
        Padding(
          padding: const EdgeInsets.only(bottom: 10),
          child: Text(
            _invokeError!,
            style: TextStyle(color: Theme.of(context).colorScheme.error),
            textAlign: TextAlign.center,
          ),
        ),
      );
    } else if (_autoReturning) {
      out.add(
        Padding(
          padding: const EdgeInsets.only(bottom: 10),
          child: Text(
            'Returning to ambient…',
            style: Theme.of(context).textTheme.bodySmall,
            textAlign: TextAlign.center,
          ),
        ),
      );
    }
    return out;
  }

  void _maybeAutoScroll() {
    if (!mounted || !_scroll.hasClients) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_scroll.hasClients) return;
      final pos = _scroll.position;
      if (pos.maxScrollExtent - pos.pixels < 120) {
        unawaited(
          _scroll.animateTo(
            pos.maxScrollExtent,
            duration: const Duration(milliseconds: 250),
            curve: Curves.easeOut,
          ),
        );
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final segments = _controller.segments;
    final extras = _invocationExtras();
    final itemCount = segments.length + extras.length;
    return Scaffold(
      appBar: AppBar(title: const Text('Sirious · Ambient'), centerTitle: true),
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
              child: itemCount == 0
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
                      controller: _scroll,
                      padding: const EdgeInsets.symmetric(
                        horizontal: 16,
                        vertical: 8,
                      ),
                      itemCount: itemCount,
                      itemBuilder: (context, i) =>
                          i < segments.length
                              ? _segmentTile(segments[i])
                              : extras[i - segments.length],
                    ),
            ),
            Padding(
              padding: const EdgeInsets.all(16),
              child: _invoking
                  ? OutlinedButton.icon(
                      style: OutlinedButton.styleFrom(
                        minimumSize: const Size(double.infinity, 52),
                      ),
                      icon: const Icon(Icons.stop),
                      label: const Text('Stop answering'),
                      onPressed: _finishNow,
                    )
                  : FilledButton.icon(
                      style: FilledButton.styleFrom(
                        minimumSize: const Size(double.infinity, 52),
                        backgroundColor: _controller.isActive
                            ? Theme.of(context).colorScheme.error
                            : null,
                      ),
                      icon: Icon(
                        _controller.isActive ? Icons.stop : Icons.graphic_eq,
                      ),
                      label: Text(
                        _controller.isActive ? 'Stop ambient' : 'Start ambient',
                      ),
                      onPressed: _toggle,
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
    this.highlighted = false,
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