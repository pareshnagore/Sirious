import 'dart:async';
import 'dart:io';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';

import '../models/latency_metrics.dart';
import '../models/session_phase.dart';
import '../models/transcript_turn.dart';
import 'aec_pipeline.dart';
import 'audio_capture_service.dart';
import 'audio_playback_service.dart';
import 'audio_route_watcher.dart';
import 'ghost_echo_detector.dart';
import 'websocket_client.dart';

/// Orchestrates WebSocket, mic capture, playback, and UI state.
class SiriousSessionController extends ChangeNotifier {
  SiriousSessionController() {
    _audioCapture = AudioCaptureService(onChunk: _onMicChunk);
    _webSocketClient = WebSocketClient(
      onJsonEvent: _onJsonEvent,
      onBinaryData: _onBinaryData,
      onDone: _onSocketDone,
      onError: _onSocketError,
    );
    _routeSub = AudioRouteWatcher.instance.routeChanges.listen(
      (_) => _onAudioRouteChanged(),
    );
  }

  late final AudioCaptureService _audioCapture;
  final AudioPlaybackService _audioPlayback = AudioPlaybackService();
  late final WebSocketClient _webSocketClient;

  /// Phase 6 Stage A: software AEC (AEC3) pipeline. Render leg fed from the
  /// playback path; capture leg filters mic audio before barge-in/WS.
  /// Spike-only for now: if the kill-switch fires (delay never valid), the
  /// stage-B decision is hard-duck fallback, per product_phases.md Phase 6.
  AecPipeline? _aecPipeline;
  AecPipeline? get aecPipeline => _aecPipeline;

  /// Exposed for the C2 invocation screen's auto-return (playback drain).
  AudioPlaybackService get audioPlayback => _audioPlayback;

  SessionPhase _phase = SessionPhase.idle;
  String? _sessionId;
  String? _errorMessage;
  String _currentUserText = '';
  String _currentAssistantText = '';
  final List<TranscriptTurn> _turns = [];
  final LatencyMetrics latency = LatencyMetrics();
  DateTime? _currentTurnStartedAt;

  /// Stable per-conversation id (protocol v2). Generated once per user session
  /// and reused across reconnects so the backend can resume the SAME Gemini
  /// session instead of starting fresh. Cleared when the user ends the
  /// session, so the next Start begins a genuinely new conversation.
  String? _clientSessionId;

  SessionPhase get phase => _phase;
  String? get sessionId => _sessionId;
  String? get errorMessage => _errorMessage;
  String get currentUserText => _currentUserText;
  String get currentAssistantText => _currentAssistantText;
  List<TranscriptTurn> get turns => List.unmodifiable(_turns);
  bool get isSessionActive => _phase.isActive;

  /// Persistent, on-screen barge-in measurement log (newest first). Rendered
  /// directly in the UI so results are visible without files/logcat/screenshots.
  final List<String> _bargeInLog = <String>[];
  List<String> get bargeInLog => List.unmodifiable(_bargeInLog);

  // ── Network-blip resilience ─────────────────────────────────────────────
  // On a brief disconnect or a stalled socket we auto-reconnect to a FRESH
  // server+Gemini session (protocol v1 has no automated resumption). Any
  // on-device transcript already committed is preserved; the mid-utterance
  // partial turn is committed before reconnect so nothing on screen is lost.
  bool _allowReconnect = false; // true only while the user wants a live session
  int _reconnectAttempts = 0;
  static const int _maxReconnectAttempts = 5;
  static const int _keepaliveIntervalMs = 5000; // ping cadence
  static const int _stallTimeoutMs = 15000; // no server data → treat as dead
  Timer? _reconnectTimer;
  Timer? _keepaliveTimer;

  /// C2 hard duck (C2.5 step 1): while the assistant is ANSWERING an
  /// invocation, do NOT stream mic audio to Gemini. On a table the speaker
  /// output would otherwise be transcribed back as a (barge-in) user turn
  /// and cut the answer off mid-sentence. Table mode has no barge-in yet —
  /// by design (ducking ladder step 1, no exceptions).
  bool _captureDucked = false;

  /// Caller's duck preference for THIS session (base state the adaptive
  /// per-turn duck restores to at turn boundaries).
  bool _captureDuckedBase = false;

  // ── Stage B (B4): route-change AEC re-init ───────────────────────────────
  // Speaker ↔ BT ↔ earphone shifts the acoustic echo path (delay + impulse
  // response). AEC3 adapts eventually, but re-creating the pipeline gives a
  // clean estimator immediately. Only rebuilt while a session is live; the
  // subscription lives for the controller's lifetime.
  StreamSubscription<void>? _routeSub;
  static const int _routeReinitCooldownMs = 3000;
  DateTime _lastRouteReinitAt = DateTime.fromMillisecondsSinceEpoch(0);
  int _routeReinitCount = 0;

  /// Backoff delay for the Nth retry (1s, 2s, 4s, … capped at 8s).
  Duration _reconnectDelay() {
    final exp = math.min(_reconnectAttempts, 3); // 2^3 = 8s cap
    return Duration(milliseconds: 1000 * (1 << exp));
  }

  static String _clock(DateTime dt) {
    String p(int v) => v.toString().padLeft(2, '0');
    final ms = dt.millisecond.toString().padLeft(3, '0');
    return '${p(dt.hour)}:${p(dt.minute)}:${p(dt.second)}.$ms';
  }

  /// Barge-in onset is detected relative to the ambient noise floor so it
  /// adapts to the device's real mic gain (AEC/AGC are device-dependent).
  /// Onset fires when: rms > max(noiseFloor * riseFactor, hardFloor).
  /// Speck chunks far above silence are excluded from the floor so speech
  /// never inflates the baseline (which would suppress detection).
  /// RECALIBRATED 30 Aug for PLATFORM AEC capture (voiceCommunication +
  /// InCommunication): probe showed room tone ~2-7, real near speech 63-120+
  /// (vs the old RAW mic's 1000-8000). 60 keeps the same speech/noise ratio
  /// the old 250 had against the raw mic.
  static const double _onsetHardFloor = 60.0;
  static const double _onsetRiseFactor = 4.0;
  static const int _noiseWindowChunks = 20; // ~2s at 100ms chunks

  /// Stage B (B3): residual-echo margin — DORMANT with platform AEC
  /// (residualFloor comes from the unwired software AEC pipeline = 0).
  /// Kept for the software-AEC backup path.
  static const double _residualRiseFactor = 8.0;

  /// Onset hard floor DURING PLAYBACK. With PLATFORM AEC (probe 30 Aug),
  /// far-end residual sits ~50 RMS and near speech at table distance 63-120+.
  /// The old 450 (raw-mic calibration) was deaf to real users — "barge-in
  /// not working". 110 clears residual (~50) with margin, sits under real
  /// speech onset at table distance.
  static const double _onsetPlaybackHardFloor = 110.0;

  /// Stage B (B3): voice-like recency window. The ghost gate accepts Gemini's
  /// `interrupted` / transcript-during-playback only if a client-side voice
  /// onset fired within this window before the server event — residual echo
  /// does NOT look like speech to the onset detector, so this is the
  /// evidence distinguishing a real user from leaked echo.
  static const int _onsetRecencyMs = 2000;

  final List<double> _recentRms = <double>[];
  double _onsetNoiseFloor = 0;
  double _windowPeakRms = 0; // loudest chunk in the CURRENT playback window
  bool _interruptionHandled = false; // true once this turn's barge-in ran
  DateTime? _lastVoiceLikeAt; // last onset crossing while playing/responding
  bool _wasLoud = false; // previous chunk crossed threshold (sustain gate)

  // ── Stage B: lexical ghost-echo detector ──────────────────────────────────
  // AEC residual at conversational volume stays intelligible enough that
  // Gemini transcribes the assistant's OWN words back as user turns (seen
  // live 29 Aug: "What can I help", "Just let me know" as You-turns). Energy
  // gating cannot separate speech-echo from speech — but CONTENT can: the
  // echo is a delayed copy of what we just played. Unit-tested against the
  // real ghost strings from that session (ghost_echo_detector_test.dart);
  // known misses (transcription variance like "Alright"→"All right") fall
  // through to the per-turn adaptive-duck backstop in _handleInterruption.
  final GhostEchoDetector _ghostDetector = GhostEchoDetector();

  double _rms(Uint8List pcm) {
    final sampleCount = pcm.length ~/ 2;
    if (sampleCount <= 0) {
      return 0;
    }
    final bd = ByteData.sublistView(pcm);
    var sumSq = 0.0;
    for (var i = 0; i < sampleCount; i++) {
      final s = bd.getInt16(i * 2, Endian.little).toDouble();
      sumSq += s * s;
    }
    return math.sqrt(sumSq / sampleCount);
  }

  /// Durable log. Dual-writes BOTH directories:
  ///   - app documents (private): historical channel
  ///   - external files dir: adb-pullable on RELEASE builds
  ///     (`adb pull /storage/emulated/0/Android/data/com.sirious.sirious/files/`)
  ///     — run-as is blocked on release, so this is the debug lifeline.
  /// `getApplicationDocumentsDirectory()` is the canonical writable app dir
  /// (Directory.systemTemp is not writable on Android).
  Future<void> _logToFile(String message) async {
    final line = '${DateTime.now().toIso8601String()} $message\n';
    try {
      final dir = await getApplicationDocumentsDirectory();
      final file = File('${dir.path}/sirious_bargein.log');
      await file.writeAsString(line, mode: FileMode.append);
    } catch (e) {
      debugPrint('bargein log write failed: $e');
    }
    try {
      final ext = await getExternalStorageDirectories();
      if (ext != null && ext.isNotEmpty) {
        final file = File('${ext.first.path}/sirious_bargein.log');
        await file.writeAsString(line, mode: FileMode.append);
      }
    } catch (_) {
      // External storage unavailable (rare) — documents copy still holds.
    }
  }

  void _logBargeIn() {
    final onset = latency.bargeInOnsetAt;
    final interrupted = latency.lastInterruptedAt;
    final stopped = latency.bargeInAudioStoppedAt;

    // Copy live levels into the persisted display fields BEFORE any reset.
    latency.lastBargeInPeakRms = _windowPeakRms;
    latency.lastBargeInNoiseFloor = _onsetNoiseFloor;

    if (onset != null && interrupted != null && stopped != null) {
      final serverMs = interrupted.difference(onset).inMilliseconds;
      final flushMs = stopped.difference(interrupted).inMilliseconds;
      final totalMs = stopped.difference(onset).inMilliseconds;
      latency.lastBargeInServerMs = serverMs;
      latency.lastBargeInFlushMs = flushMs;
      latency.lastBargeInTotalMs = totalMs;
      latency.lastBargeInOnsetMissing = false;
      debugPrint(
        'BARGE_IN onset→interrupt(server+net)=${serverMs}ms · '
        'interrupt→stop(flush)=${flushMs}ms · TOTAL=${totalMs}ms',
      );
      unawaited(
        _logToFile(
          'BARGE_IN ok onset_ms=$serverMs flush_ms=$flushMs total_ms=$totalMs '
          'peak_rms=${_windowPeakRms.toStringAsFixed(0)} '
          'floor_rms=${_onsetNoiseFloor.toStringAsFixed(0)}',
        ),
      );
      _pushBargeInLine(
        '${_clock(onset)} → ${_clock(stopped)} · $totalMs ms '
        '(server $serverMs + flush $flushMs)',
      );
    } else {
      latency.lastBargeInTotalMs = null;
      latency.lastBargeInOnsetMissing = true;
      debugPrint(
        'BARGE_IN interrupted; onset missing '
        '(peakRms=${latency.lastBargeInPeakRms.toStringAsFixed(0)})',
      );
      unawaited(
        _logToFile(
          'BARGE_IN ONSET_MISSING peak_rms=${_windowPeakRms.toStringAsFixed(0)} '
          'floor_rms=${_onsetNoiseFloor.toStringAsFixed(0)} '
          'onset=$onset interrupted=$interrupted stopped=$stopped',
        ),
      );
      _pushBargeInLine(
        '${_clock(DateTime.now())} · onset missed '
        '(peak ${_windowPeakRms.toStringAsFixed(0)} '
        'floor ${_onsetNoiseFloor.toStringAsFixed(0)})',
      );
    }
    notifyListeners();
  }

  void _pushBargeInLine(String line) {
    _bargeInLog.insert(0, line);
    // 100 lines: deep enough to cover a full multi-test session without
    // wrapping off the evidence (was 12 — screenshots kept losing context).
    if (_bargeInLog.length > 100) {
      _bargeInLog.removeLast();
    }
  }

  /// Single code path for a barge-in, triggered either by Gemini's explicit
  /// `interrupted` event OR by a `user_transcript` arriving during playback.
  /// Runs at most once per turn (guarded by [_interruptionHandled]).
  ///
  /// Stage B (B3) GHOST GATE: with capture open during playback, leaked echo
  /// can still trip Gemini's VAD server-side even after AEC. A REAL user
  /// produces client-side voice-like energy (onset threshold crossed) shortly
  /// before the server event; residual echo does not. So an interruption
  /// signal with no voice-like onset in the last [_onsetRecencyMs] is treated
  /// as a ghost: logged, playback kept, turn NOT cut.
  Future<void> _handleInterruption(DateTime receivedAt) async {
    if (_interruptionHandled) {
      return;
    }
    final lastVoice = _lastVoiceLikeAt;
    final voiceRecent =
        lastVoice != null &&
        receivedAt.difference(lastVoice).inMilliseconds <= _onsetRecencyMs;
    if (!voiceRecent) {
      unawaited(
        _logToFile(
          'GHOST_REJECT ${_clock(receivedAt)} '
          'res=${(_aecPipeline?.residualFloor ?? 0).toStringAsFixed(0)} '
          'lastVoice=${lastVoice == null ? 'never' : _clock(lastVoice)} '
          '→ duck rest of turn',
        ),
      );
      _pushBargeInLine(
        '${_clock(receivedAt)} · ghost rejected — ducking rest of turn',
      );
      // Echo is reaching Gemini's VAD with no local voice evidence. Duck the
      // REST of this turn (Stage A behavior) so the flush→echo→loop cannot
      // sustain itself; _endOnsetWindow() at the next turn boundary restores
      // the caller's base duck state. A real user mid-duck rides the same
      // path as table mode: their turn lands after turn_complete.
      _captureDucked = true;
      return;
    }
    _interruptionHandled = true;

    latency.lastInterruptedAt = receivedAt;
    _setPhase(SessionPhase.interrupting);

    DateTime? stoppedAt;
    try {
      stoppedAt = await _audioPlayback.flush();
    } catch (error, stackTrace) {
      debugPrint('Barge-in flush failed: $error\n$stackTrace');
    }
    latency.bargeInAudioStoppedAt = stoppedAt ?? DateTime.now();

    _logBargeIn();
    _endOnsetWindow();
    _commitCurrentTurn(interrupted: true);
    _setPhase(SessionPhase.listening);
    latency.resetForTurn();
  }

  /// Phase 6 Stage B→C: PLATFORM AEC is the primary echo canceller on the
  /// speaker path (probed working 30 Aug via VOICE_COMMUNICATION +
  /// setCommunicationDevice). The software AEC3 pipeline stays in the
  /// codebase as BACKUP (vendor-parity: ChatGPT keeps WebRTC APM behind the
  /// platform AEC) but is UNWIRED from the capture path — metrics only, no
  /// gating decisions depend on it. Flip `_useSoftwareAec` to re-enable.
  static const bool _useSoftwareAec = false;

  Future<void> startSession({
    String? seed,
    String? invoke,
    bool duckCapture = false,
    String? clientSessionId,
  }) async {
    if (_phase.isActive) {
      return;
    }

    _errorMessage = null;
    _sessionId = null;
    // Phase 5 C+B: an ambient invocation REUSES the ambient conversation
    // id, so the backend continues the SAME Firestore doc (room turns +
    // answers in one History entry). Normal 1:1 mode generates a fresh id.
    _clientSessionId = clientSessionId ?? _generateClientSessionId();
    _turns.clear();
    _currentUserText = '';
    _currentAssistantText = '';
    _currentTurnStartedAt = null;
    latency.sessionConnectedAt = null;
    latency.firstMicChunkAt = null;
    latency.resetForTurn();
    _allowReconnect = true;
    _reconnectAttempts = 0;
    // Stage B (B1): ducking is caller-controlled again. Stage A's hardcoded
    // `true` is gone — AEC3 is proven on-device (kill-switch passed), so
    // capture stays OPEN during playback on the speaker path and the
    // residual-based ghost gate (below) guards against echo-driven
    // self-interruption instead of ducking by construction.
    _captureDucked = duckCapture;
    _captureDuckedBase = duckCapture;
    _setPhase(SessionPhase.connecting);

    // Stage B→C (30 Aug): PLATFORM AEC path. Capture profile routes the mic
    // through Android's AcousticEchoCanceler with our playout as far-end
    // reference (full-duplex, probe-verified). The software AEC3 pipeline is
    // unwired (backup only — see _useSoftwareAec).
    if (_useSoftwareAec) {
      try {
        _aecPipeline ??= AecPipeline(
          onStatsLine: (line) {
            _pushBargeInLine(line);
            unawaited(_logToFile(line));
          },
        );
        _aecPipeline!.resetMetrics();
        _connectAecTap();
      } catch (error) {
        // No native lib (or stub ABI) → run without AEC, same as pre-spike.
        _aecPipeline = null;
        unawaited(_logToFile('AEC init failed: $error'));
      }
    }

    try {
      await _audioPlayback.init();
      await _webSocketClient.connect(
        clientSessionId: _clientSessionId,
        seed: seed,
        invoke: invoke,
      );
      await _audioCapture.start(
        profile: duckCapture ? CaptureProfile.nearTalk : CaptureProfile.speaker,
      );
      _startKeepalive();
      _setPhase(SessionPhase.listening);
    } catch (error) {
      _allowReconnect = false;
      _errorMessage = error.toString();
      _setPhase(SessionPhase.error);
      await _cleanupSession(endPhase: SessionPhase.error);
    }
  }

  Future<void> endSession() async {
    if (!_phase.isActive && _phase != SessionPhase.error) {
      return;
    }

    _setPhase(SessionPhase.ending);
    await _cleanupSession(endPhase: SessionPhase.idle);
  }

  Future<void> _cleanupSession({required SessionPhase endPhase}) async {
    _allowReconnect = false;
    _captureDucked = false;
    _stopKeepalive();
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _commitCurrentTurn(interrupted: _phase == SessionPhase.interrupting);

    try {
      if (_webSocketClient.isConnected) {
        _webSocketClient.sendStop();
      }
    } catch (_) {
      // Best effort.
    }

    await _audioCapture.stop();
    await _webSocketClient.disconnect();
    // Keep the native audio engine warm across sessions (only clear its queues).
    // Calling dispose() here tears down playback, and the plugin does not reset
    // its _needsStart flag on re-setup — the next session would have no audio.
    await _audioPlayback.flush();

    _sessionId = null;
    _clientSessionId = null;
    _currentUserText = '';
    _currentAssistantText = '';
    _currentTurnStartedAt = null;
    _setPhase(endPhase);
  }

  void _onMicChunk(Uint8List chunk) {
    latency.firstMicChunkAt ??= DateTime.now();

    // Phase 6 Stage A: AEC processes the mic BEFORE anything else consumes
    // it, so barge-in onset and Gemini both see echo-cancelled audio.
    final aec = _aecPipeline;
    if (aec != null) {
      final processed = aec.processCaptureChunk(chunk);
      if (chunk.isNotEmpty && processed.isEmpty) {
        // Sub-frame remainder buffered; nothing new to send this tick.
        return;
      }
      chunk = processed;
    }

    // C2 hard duck: while the assistant is answering an invocation, do NOT
    // stream mic audio (speaker echo would be transcribed back as a user
    // turn and cut the answer off). No barge-in in ducked mode — the user's
    // request already rode the invoke text; follow-ups work after
    // turn_complete (phase returns to listening and capture resumes).
    //
    // Phase 6 Stage B: the duck now applies ONLY to table/ambient invocation
    // (duckCapture: true). On the speaker path capture stays OPEN during
    // playback — the PLATFORM AEC removes the echo at the HAL layer
    // (voiceCommunication + setCommunicationDevice, probed 30 Aug), so no
    // ducking, no software gating on the audio path. The client-side gates
    // below remain as belt-and-suspenders for residual echo.
    if (_captureDucked &&
        (_phase == SessionPhase.playing ||
            _phase == SessionPhase.responding ||
            _phase == SessionPhase.interrupting)) {
      return;
    }

    // Barge-in onset: track mic energy while Sirious is talking (including the
    // brief `interrupting` window so a fast server response can't suppress it).
    // A single chunk above the adaptive threshold marks the user's onset.
    if (_phase == SessionPhase.playing ||
        _phase == SessionPhase.responding ||
        _phase == SessionPhase.interrupting) {
      final rms = _rms(chunk);
      if (rms > _windowPeakRms) {
        _windowPeakRms = rms;
      }

      // Update the quiet baseline only with near-silence samples.
      if (rms < _onsetHardFloor * 2) {
        _recentRms.add(rms);
        if (_recentRms.length > _noiseWindowChunks) {
          _recentRms.removeAt(0);
        }
      }
      var floor = double.infinity;
      for (final v in _recentRms) {
        if (v < floor) {
          floor = v;
        }
      }
      _onsetNoiseFloor = floor.isFinite ? floor : _onsetHardFloor;
      latency.lastBargeInNoiseFloor = _onsetNoiseFloor;

      // Stage B (B3): while Sirious plays, the threshold must also clear the
      // residual echo floor at the current volume (measured post-AEC by the
      // pipeline, ~1s window) and a higher hard floor (echo bursts measured
      // 266-521 RMS while real speech at speaker distance is 1000+). In
      // listening phase residual is 0 and the normal floor applies.
      final residual = _aecPipeline?.residualFloor ?? 0;
      final hardFloor = _onsetPlaybackHardFloor;
      final threshold = math.max(
        math.max(
          _onsetNoiseFloor * _onsetRiseFactor,
          residual * _residualRiseFactor,
        ),
        hardFloor,
      );

      // Sustained-voice gate (Stage B, log-driven 30 Aug): post-AEC echo
      // arrives in TRANSIENTS — per-chunk RMS 3677-7033 while the residual
      // floor sits at 11 (75%+ of echo chunks are near-silence) — and a
      // single loud burst used to count as "voice evidence". Real speech
      // sustains. So while the far end is active (or in its ~400 ms tail),
      // a chunk only counts as voice if the ENERGY ALSO HELD last chunk —
      // i.e. two consecutive loud chunks ≈ 200 ms of continuous voice.
      // First-burst echo now fails the gate; Gemini-side echoes never get
      // "voice evidence", and the burst itself still streams (harmless
      // residual noise for the server VAD, cleaned by the lexical filter).
      final sustained = aec == null || !aec.farEndRecently || _wasLoud;
      if (rms > threshold) {
        if (sustained) {
          if (latency.bargeInOnsetAt == null) {
            latency.bargeInOnsetAt = DateTime.now();
            unawaited(
              _logToFile(
                'ONSET rms=${rms.toStringAsFixed(0)} '
                'thr=${threshold.toStringAsFixed(0)} floor=${_onsetNoiseFloor.toStringAsFixed(0)} '
                'res=${residual.toStringAsFixed(0)} sustained=true',
              ),
            );
          }
          // Voice-like energy just happened — feeds the ghost gate.
          _lastVoiceLikeAt = DateTime.now();
        } else {
          unawaited(
            _logToFile(
              'BURST_SKIP rms=${rms.toStringAsFixed(0)} '
              'thr=${threshold.toStringAsFixed(0)} res=${residual.toStringAsFixed(0)} '
              '(transient during playback — not voice)',
            ),
          );
        }
      }
      _wasLoud = rms > threshold;
    }

    if (_webSocketClient.isConnected) {
      _webSocketClient.sendAudio(chunk);
    }
  }

  void _onBinaryData(Uint8List chunk) {
    latency.firstAssistantAudioAt ??= DateTime.now();

    if (_phase == SessionPhase.listening || _phase == SessionPhase.responding) {
      _setPhase(SessionPhase.playing);
    }

    _audioPlayback.enqueue(chunk);
  }

  /// Wire the AEC far-end reference to the playback drain loop (pre-feed).
  /// Called once after the pipeline is created.
  void _connectAecTap() {
    _audioPlayback.playbackTap = (chunk) => _aecPipeline?.feedRender(chunk);
  }

  /// Stage B (B4): the audio output route changed → rebuild the AEC pipeline
  /// so the delay estimator re-locks on the new echo path instead of grinding
  /// through a re-adaptation transient (or diverging on a big delay jump).
  void _onAudioRouteChanged() {
    if (_phase != SessionPhase.playing &&
        _phase != SessionPhase.responding &&
        _phase != SessionPhase.listening &&
        _phase != SessionPhase.interrupting) {
      return; // no live session → next startSession() builds fresh anyway
    }
    final now = DateTime.now();
    if (now.difference(_lastRouteReinitAt).inMilliseconds <
        _routeReinitCooldownMs) {
      return; // coalesce duplicate native callbacks
    }
    _lastRouteReinitAt = now;
    _routeReinitCount++;

    try {
      _aecPipeline?.dispose();
    } catch (_) {}
    try {
      _aecPipeline = AecPipeline(
        onStatsLine: (line) {
          _pushBargeInLine(line);
          unawaited(_logToFile(line));
        },
      );
      _connectAecTap();
      final line =
          'ROUTE re-init #$_routeReinitCount — AEC rebuilt for new output route';
      _pushBargeInLine(line);
      unawaited(_logToFile(line));
    } catch (error) {
      // Route change hit while the native lib is unavailable — drop the AEC
      // leg and keep the session alive (same policy as init failure).
      _aecPipeline = null;
      unawaited(_logToFile('AEC route re-init failed: $error'));
    }
    notifyListeners();
  }

  Future<void> _onJsonEvent(Map<String, dynamic> event) async {
    final type = event['type'] as String?;
    debugPrint('WS_EVENT: $type');
    unawaited(_logToFile('EVENT $type ${event['text'] ?? ''}'));

    switch (type) {
      case 'session_started':
        _sessionId = event['session_id'] as String?;
        latency.sessionConnectedAt = DateTime.now();
        final resumed = event['resumed'] == true;
        if (_phase == SessionPhase.reconnecting) {
          // Reconnect succeeded → resume listening on the fresh session.
          final recovered = _reconnectAttempts;
          _reconnectAttempts = 0;
          _pushBargeInLine(
            resumed
                ? 'Reconnected — Gemini context RESUMED '
                      '(after $recovered retr${recovered == 1 ? 'y' : 'ies'})'
                : 'Reconnected after $recovered retr'
                      '${recovered == 1 ? 'y' : 'ies'} (fresh Gemini context)',
          );
          unawaited(
            _logToFile(
              'RECONNECT ok attempts=$recovered new_session=$_sessionId '
              'resumed=$resumed',
            ),
          );
          _setPhase(SessionPhase.listening);
        } else {
          unawaited(_logToFile('SESSION started=$_sessionId resumed=$resumed'));
        }
        break;

      case 'user_transcript':
        final text = event['text'] as String? ?? '';
        if (text.isEmpty) {
          break;
        }

        // Post-turn echo guard: the assistant's own tail can keep arriving
        // through the AEC residual for a moment AFTER playback ends. A
        // "user" transcript that repeats just-spoken assistant words within
        // the guard window is echo — do not open a user turn with it.
        if (_phase == SessionPhase.listening &&
            _ghostDetector.isWithinPostTurnGuard &&
            _ghostDetector.isEcho(text)) {
          unawaited(
            _logToFile(
              'GHOST_LEXICAL ${_clock(DateTime.now())} post-turn '
              '"${text.length > 40 ? '${text.substring(0, 40)}…' : text}" → dropped',
            ),
          );
          _pushBargeInLine('ghost echo rejected (post-turn): "$text"');
          break;
        }

        latency.firstUserTranscriptAt ??= DateTime.now();
        _currentTurnStartedAt ??= DateTime.now();
        _currentUserText += text;

        if (_phase == SessionPhase.playing) {
          // User is talking while Sirious is playing — that is a barge-in…
          // UNLESS the "user" text is the assistant's own echo. Lexical
          // ghost check BEFORE any flush: energy gates cannot separate
          // speech-echo from speech (echo IS speech), content can.
          if (_ghostDetector.isEcho(text)) {
            unawaited(
              _logToFile(
                'GHOST_LEXICAL ${_clock(DateTime.now())} '
                '"${text.length > 40 ? '${text.substring(0, 40)}…' : text}" '
                'matches recent assistant words → dropped',
              ),
            );
            _pushBargeInLine('ghost echo rejected: "$text"');
            // The energy crossing that just happened was echo, not voice —
            // cancel it so a trailing `interrupted` event finds no voice
            // evidence and is ghost-rejected (with per-turn duck) too.
            _lastVoiceLikeAt = null;
            latency.bargeInOnsetAt = null;
            break;
          }
          // User is talking while Sirious is playing — that is a barge-in.
          // Flush + measure immediately; don't wait for a separate
          // `interrupted` event (Gemini may not always emit one).
          await _handleInterruption(DateTime.now());
        } else if (_phase == SessionPhase.listening) {
          _setPhase(SessionPhase.responding);
        }

        notifyListeners();
        break;

      case 'assistant_transcript':
        final text = event['text'] as String? ?? '';
        if (text.isEmpty) {
          break;
        }

        latency.firstAssistantTranscriptAt ??= DateTime.now();
        _currentTurnStartedAt ??= DateTime.now();
        _currentAssistantText += text;
        _ghostDetector.trackAssistant(text);

        if (_phase == SessionPhase.listening ||
            _phase == SessionPhase.responding) {
          _setPhase(SessionPhase.playing);
        }

        notifyListeners();
        break;

      case 'response_finished':
        if (_phase == SessionPhase.responding) {
          _setPhase(SessionPhase.playing);
        }
        break;

      case 'interrupted':
        await _handleInterruption(DateTime.now());
        break;

      case 'turn_complete':
        _endOnsetWindow();
        _commitCurrentTurn(interrupted: false);
        _setPhase(SessionPhase.listening);
        latency.resetForTurn();
        _ghostDetector.markTurnComplete();
        break;

      case 'session_warning':
        _errorMessage =
            'Session warning: ${event['code']} (${event['time_left']})';
        notifyListeners();
        break;

      case 'error':
        _errorMessage = '${event['code']}: ${event['message']}';
        _setPhase(SessionPhase.error);
        notifyListeners();
        break;

      default:
        break;
    }
  }

  void _onSocketDone() {
    if (_phase.isActive && _allowReconnect) {
      // Network blip (server closed / lost) → reconnect instead of ending.
      _scheduleReconnect();
    } else if (_phase.isActive) {
      _errorMessage ??= 'Connection closed';
      unawaited(_cleanupSession(endPhase: SessionPhase.idle));
    }
  }

  void _onSocketError(Object error) {
    if (_phase.isActive && _allowReconnect) {
      unawaited(_logToFile('RECONNECT socket_error $_errorMessage'));
      _scheduleReconnect();
      return;
    }
    _errorMessage = error.toString();
    _setPhase(SessionPhase.error);
    notifyListeners();
    unawaited(_cleanupSession(endPhase: SessionPhase.error));
  }

  // ── Network-blip reconnect ──────────────────────────────────────────────

  /// Commit the in-flight partial turn (so nothing on screen is lost), stop
  /// playback, then schedule the next connect attempt with backoff.
  void _scheduleReconnect() {
    if (!_allowReconnect) {
      return;
    }
    if (_reconnectAttempts >= _maxReconnectAttempts) {
      _errorMessage =
          'Connection lost. Could not reconnect after '
          '$_maxReconnectAttempts attempts.';
      _setPhase(SessionPhase.error);
      notifyListeners();
      unawaited(_cleanupSession(endPhase: SessionPhase.error));
      return;
    }

    _commitCurrentTurn(interrupted: false);
    _endOnsetWindow();
    try {
      _audioPlayback.flush();
    } catch (_) {}

    _setPhase(SessionPhase.reconnecting);
    _reconnectAttempts++;
    final delay = _reconnectDelay();
    unawaited(
      _logToFile(
        'RECONNECT schedule attempt=$_reconnectAttempts delay_ms=${delay.inMilliseconds}',
      ),
    );
    _pushBargeInLine('Blip — reconnecting (attempt $_reconnectAttempts)…');
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(delay, () {
      _reconnectTimer = null;
      unawaited(_tryReconnect());
    });
  }

  Future<void> _tryReconnect() async {
    try {
      // Same client_session_id → backend resumes the SAME Gemini session
      // (model memory intact) when it holds a live resumption handle.
      await _webSocketClient.connect(clientSessionId: _clientSessionId);
      // On success we stay in `reconnecting` until `session_started` flips us
      // to `listening`. If this attempt drops again, onDone schedules the next.
    } catch (error) {
      unawaited(_logToFile('RECONNECT attempt failed: $error'));
      _scheduleReconnect();
    }
  }

  /// Stable id for the current conversation, sent on every (re)connect so the
  /// backend can look up the Gemini resumption handle for continuity.
  String _generateClientSessionId() {
    final ts = DateTime.now().millisecondsSinceEpoch.toRadixString(36);
    final rand = math.Random().nextInt(0x7FFFFFFF).toRadixString(36);
    return 'cs-$ts-$rand';
  }

  // ── Keepalive / stall watchdog ──────────────────────────────────────────
  // A "blip" can also silently stall the socket (still connected, no data).
  // We ping periodically and salvage if the server stops sending entirely.

  void _startKeepalive() {
    _keepaliveTimer?.cancel();
    _keepaliveTimer = Timer.periodic(
      const Duration(milliseconds: _keepaliveIntervalMs),
      (_) => _keepaliveTick(),
    );
  }

  void _stopKeepalive() {
    _keepaliveTimer?.cancel();
    _keepaliveTimer = null;
  }

  void _keepaliveTick() {
    if (!_allowReconnect) {
      return;
    }
    if (_webSocketClient.isConnected) {
      try {
        _webSocketClient.sendPing();
      } catch (_) {}
    }

    final sinceData = DateTime.now().difference(
      _webSocketClient.lastReceivedAt,
    );
    final phase = _phase;
    final isLivePhase =
        phase == SessionPhase.listening ||
        phase == SessionPhase.responding ||
        phase == SessionPhase.playing;
    if (isLivePhase && sinceData.inMilliseconds > _stallTimeoutMs) {
      unawaited(
        _logToFile(
          'RECONNECT stall detected idle_ms=${sinceData.inMilliseconds}',
        ),
      );
      // Tear down the stale socket — its cancelled subscription won't fire
      // onDone, so schedule the reconnect explicitly.
      unawaited(_webSocketClient.disconnect());
      _scheduleReconnect();
    }
  }

  void _endOnsetWindow() {
    // Reset only the LIVE window tracking. Persisted display values
    // (lastBargeIn*) must survive so the summary keeps the last result.
    _recentRms.clear();
    _onsetNoiseFloor = 0;
    _windowPeakRms = 0;
    _lastVoiceLikeAt = null; // ghost gate: stale voice evidence must not count
    _wasLoud = false; // sustain gate: a stale "was loud" must not carry over
    _interruptionHandled = false;
    // Adaptive duck (Stage B ghost-gate fallback): restore the caller's base
    // state — a ghost-triggered per-turn duck never survives the turn boundary.
    _captureDucked = _captureDuckedBase;
  }

  void _commitCurrentTurn({required bool interrupted}) {
    final user = _currentUserText.trim();
    final assistant = _currentAssistantText.trim();

    if (user.isEmpty && assistant.isEmpty) {
      _currentUserText = '';
      _currentAssistantText = '';
      _currentTurnStartedAt = null;
      return;
    }

    _turns.add(
      TranscriptTurn(
        userText: user,
        assistantText: assistant,
        interrupted: interrupted,
        startedAt: _currentTurnStartedAt ?? DateTime.now(),
        completedAt: DateTime.now(),
      ),
    );

    _currentUserText = '';
    _currentAssistantText = '';
    _currentTurnStartedAt = null;
    notifyListeners();
  }

  void _setPhase(SessionPhase next) {
    if (_phase == next) {
      return;
    }

    _phase = next;
    notifyListeners();
  }

  @override
  void dispose() {
    unawaited(_routeSub?.cancel());
    unawaited(_cleanupSession(endPhase: SessionPhase.idle));
    unawaited(_audioCapture.dispose());
    _aecPipeline?.dispose();
    _aecPipeline = null;
    super.dispose();
  }
}
