import 'dart:async';
import 'dart:io';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';

import '../models/latency_metrics.dart';
import '../models/session_phase.dart';
import '../models/transcript_turn.dart';
import 'aec_pipeline.dart';
import 'adaptive_vad_policy.dart';
import 'audio_capture_service.dart';
import 'audio_playback_service.dart';
import 'audio_route_watcher.dart';
import 'capture_route_policy.dart';
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
    _routeSub = AudioRouteWatcher.instance.routeChanges.listen((_) {
      unawaited(_onAudioRouteChanged());
    });
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

  // ── Stage C (1): route-aware capture profiles ────────────────────────────
  // The ACTIVE capture profile follows the AUDIO ROUTE (Perplexity's
  // AudioCommunicationRoutePolicy pattern): earphones/BT connected →
  // nearTalk (raw MIC — the mic physically can't hear the speaker; best
  // quality; server VAD works as-is), builtin speaker → the platform-AEC
  // profile. A route-CLASS change MID-SESSION restarts capture on the new
  // profile (cooldown-coalesced). The subscription lives for the
  // controller's lifetime.
  StreamSubscription<void>? _routeSub;
  static const int _routeReinitCooldownMs = 3000;
  DateTime _lastRouteReinitAt = DateTime.fromMillisecondsSinceEpoch(0);
  int _routeReinitCount = 0;

  /// Output route the CURRENT capture profile was built for. Detected at
  /// session start and re-classified on every route_changed event.
  AudioRoute _activeRoute = AudioRoute.speaker;
  AudioRoute get activeRoute => _activeRoute;

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

  /// Barge-in onset is detected relative to the AMBIENT noise floor so it
  /// adapts to the device's real mic gain (AEC/AGC are device-dependent).
  /// Onset fires when: rms > max(noiseFloor * riseFactor, hardFloor).
  /// Speck chunks far above silence are excluded from the floor so speech
  /// never inflates the baseline (which would suppress detection).
  ///
  /// Step 4.5 (manual-VAD speaker): the DECISION is adaptive
  /// ([_adaptiveOnset] — room term + measured-residual term, 80–150 band);
  /// this getter is now only the FIXED speech-exclusion floor (route-based,
  /// post-gain) used for window admission and the legacy server-VAD paths.
  double get _onsetHardFloor =>
      CaptureRoutePolicy.onsetHardFloor(_activeRoute);

  /// Live ambient estimate for threshold math (source of truth is the
  /// near-silence window; [_onsetNoiseFloor] is its logging mirror).
  double get _ambientFloor => AdaptiveVadPolicy.ambientFloor(_recentRms);

  /// Current adaptive onset decision for the ACTIVE phase (manual VAD):
  /// listening → the room term; playback → also the measured-residual term,
  /// with the room term clamped ([AdaptiveVadPolicy.playbackAmbientCeiling])
  /// so a stale loud-window cannot push the playback floor past real speech.
  double get _adaptiveOnset {
    if (_phase == SessionPhase.playing ||
        _phase == SessionPhase.responding ||
        _phase == SessionPhase.interrupting) {
      return AdaptiveVadPolicy.playbackOnset(_ambientFloor, _playbackResidual);
    }
    return AdaptiveVadPolicy.listeningOnset(_ambientFloor);
  }

  static const double _onsetRiseFactor = 4.0;
  static const int _noiseWindowChunks = 20; // ~2s at 100ms chunks

  /// Stage B (B3): residual-echo margin — DORMANT with platform AEC
  /// (residualFloor comes from the unwired software AEC pipeline = 0).
  /// Kept for the software-AEC backup path.
  static const double _residualRiseFactor = 8.0;

  /// Barge-in onset hard floor DURING PLAYBACK, per route AND gating mode.
  /// - Manual VAD (speaker): superseded by the ADAPTIVE playback decision
  ///   (AdaptiveVadPolicy.playbackOnset — room term + measured residual
  ///   term). Fixed floors are room-calibrated by definition (31 Aug: fixed
  ///   150 false-fired on loud-room residual 84–168; the earlier fixed 450
  ///   missed real 255–305 barge-ins). This getter remains only for the
  ///   server-VAD paths.
  /// - Server-VAD paths (earphone, software-AEC, ducked): unchanged 450 —
  ///   there the server decides and the 450 is the validated calibration.
  double get _onsetPlaybackHardFloor =>
      CaptureRoutePolicy.onsetPlaybackHardFloor(_activeRoute);

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

  // ── Step 4.6: playback windows open on SUSTAINED voice only ─────────────
  // The 17:00 loud-room loop (echo transient 170-206 > 150 clamp): EVERY
  // false window opened on a SINGLE chunk and closed with speechChunks=1 —
  // the open window streamed the echo residue server-side → the model
  // answered its own echo → next answer's echo → repeat. Real speech
  // sustains multiple chunks. So a playback window may open only when
  // chunk 1 clears the onset threshold AND chunk 2 stays above the hold
  // level (onset − 20). Listening phase keeps the single-chunk open (turn
  // starts stay fast; quiet single-chunk speech like the 94-RMS "Nein."
  // must not be eaten).
  bool _prevChunkAboveHold = false;

  // ── Step 4.5: adaptive voice levels (manual-VAD speaker path) ───────────
  // Rolling ~1 s of playback-phase chunk RMS. [_playbackResidual] (25th
  // percentile) measures the room's amplified residual — the floor the
  // playback onset must clear. PERSISTS across turns so each answer is
  // seeded with the last answer's echo (a fresh answer otherwise rides the
  // first ~1 s blind).
  final List<double> _playbackResidualWindow = <double>[];
  double _playbackResidual = 0;

  /// Onset decision used for the PREVIOUS chunk — the hold level rides it
  /// (hysteresis must track the CURRENT decision, not a stale one).
  double _prevOnsetDecision = AdaptiveVadPolicy.playbackLowerClamp;

  /// Chunk-RMS admission bar for the near-silence window (both paths):
  /// the FIXED route floor ×2. Manual VAD must NOT admit by the adaptive
  /// value — self-referential when the signal it measures can enter the
  /// window (loud-room residual would ratchet the "ambient" up to itself).
  double get _ambientAdmissionFloor => _onsetHardFloor;

  // ── Step 4: manual client VAD (speaker route) ───────────────────────────
  // With automaticActivityDetection disabled, GEMINI NEVER DECIDES — it
  // answers/resumes ONLY on our activityStart/activityEnd. That removes the
  // server-side ghost class (30 Aug: the model interrupted itself on the
  // amplified residual of ONE of its own words — no client floor can prevent
  // that, because the server hears what it hears). Turn boundaries and
  // barge-in decisions move to the client onset detector.
  //
  // Design (official Live API docs, read 30 Aug — "Disable automatic VAD"):
  // client sends activityStart/activityEnd; NO audioStreamEnd in this mode;
  // an activityEnd marks the interruption. Speech STREAMS from window-open
  // (never before): client events cannot un-hear already-streamed audio, so
  // gating the residue is what protects the server's "who spoke" perception.
  // During the open window the stream stays open (platform AEC handles echo;
  // lexical ghost filter remains the display backstop).
  // Scope: speaker route only, software-AEC OFF, NOT ducked. Earphones stay
  // on server VAD (verified good — do not disturb); a half-gated stream
  // would re-starve the server VAD on the software-AEC path.
  // Onset ratio/sustain/ghost machinery: UNCHANGED (gain cancels in ratios).
  // Q1 (cancel on activityStart vs only activityEnd) settled by headless
  // probe before flashing; both orderings work with the flow below.
  static const bool _manualVadEnabled = true;
  bool get _manualVadActive =>
      _manualVadEnabled &&
      _activeRoute == AudioRoute.speaker &&
      !_useSoftwareAec &&
      !_captureDucked;

  /// The single OPEN VAD window. null = closed = stream gated.
  DateTime? _vadOpenedAt;
  int _vadSpeechChunks = 0;
  int _vadSilenceChunks = 0;

  /// Consecutive gated (no-window) chunks — diagnostics for the one real
  /// manual-VAD failure mode: a threshold so high the gate eats real speech.
  int _vadGatedChunks = 0;
  bool _vadStallLogged = false;

  static const int _vadEndOfSpeechChunks = 8; // ~800 ms trailing silence
  static const int _vadStallLogChunks = 30; // ~3 s gated with no window

  /// In-window HOLD level: the close decision uses this, NOT the onset
  /// threshold. 31 Aug 00:00 session failure: windows closed 0.6-1.5 s into
  /// sentences because natural speech dips below the 240 ONSET level between
  /// syllables (VAD_GATE fired every time) — onset thresholds are calibrated
  /// to separate speech from silence, not to mark ongoing speech. Hold level
  /// sits clearly above the adaptive floor (word gaps hold the window) and
  /// clearly below onset (true silence closes it). Hysteresis, textbook.
  /// Step 4.5: ADAPTIVE — onset decision − 20. Below onset in every room by
  /// construction (an ambient-derived hold let loud-room residual push hold
  /// ABOVE onset — windows closed mid-speech; unit tests caught it).
  double get _vadHoldLevel => AdaptiveVadPolicy.holdLevel(_prevOnsetDecision);

  // Level stamp (gain validation): first ~2 s of chunks, once per session.
  bool _levelStampDone = false;
  double _levelSumSq = 0;
  int _levelChunks = 0;

  /// Open the VAD window (idempotent) — sends activityStart.
  void _vadOpen(double rms) {
    if (_vadOpenedAt != null) {
      return;
    }
    _vadOpenedAt = DateTime.now();
    _vadSpeechChunks = 0;
    _vadSilenceChunks = 0;
    _vadGatedChunks = 0;
    _vadStallLogged = false;
    try {
      _webSocketClient.sendActivityStart();
    } catch (_) {
      // Socket down: the window still opens (stream is local); the server
      // never sees activity and the end-of-speech path closes it. Recovered
      // sessions re-open on the next onset.
    }
    final openLine =
        'VAD_OPEN route=$_activeRoute rms=${rms.toStringAsFixed(0)} '
        'floor=${_onsetNoiseFloor.toStringAsFixed(0)} '
        'thr=${_adaptiveOnset.toStringAsFixed(0)}';
    _pushBargeInLine(openLine);
    unawaited(_logToFile(openLine));
  }

  /// Close the VAD window (idempotent) — sends activityEnd.
  void _vadClose({required String reason}) {
    final openedAt = _vadOpenedAt;
    if (openedAt == null) {
      return;
    }
    _vadOpenedAt = null;
    _vadSilenceChunks = 0;
    try {
      _webSocketClient.sendActivityEnd();
    } catch (_) {
      // Socket down — nothing to signal; the server session is gone anyway.
    }
    final closeLine =
        'VAD_CLOSE route=$_activeRoute reason=$reason '
        'durationMs=${DateTime.now().difference(openedAt).inMilliseconds} '
        'speechChunks=$_vadSpeechChunks';
    unawaited(_logToFile(closeLine));
    _pushBargeInLine(closeLine);
  }

  /// Per-chunk VAD window bookkeeping (call for EVERY mic chunk when manual
  /// VAD is active). [rms] = this chunk's level; close decisions use the
  /// HOLD level (hysteresis), not the onset threshold — word gaps must hold
  /// the window (31 Aug 00:00 lesson), true trailing silence closes it.
  void _vadTick({required bool speech, required double rms}) {
    if (_vadOpenedAt != null) {
      if (rms > _vadHoldLevel) {
        _vadSpeechChunks++;
        _vadSilenceChunks = 0;
      } else {
        _vadSilenceChunks++;
        if (_vadSilenceChunks >= _vadEndOfSpeechChunks) {
          _vadClose(reason: 'end_of_speech');
        }
      }
    } else {
      _vadGatedChunks = speech ? 0 : _vadGatedChunks + 1;
      if (_vadGatedChunks >= _vadStallLogChunks && !_vadStallLogged) {
        _vadStallLogged = true;
        unawaited(
          _logToFile(
            'VAD_GATE ~3s gated with no window — if the user IS speaking, '
            'the onset threshold is eating real speech',
          ),
        );
      }
    }
  }
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
          'BARGE_IN ok route=$_activeRoute onset_ms=$serverMs flush_ms=$flushMs '
          'total_ms=$totalMs '
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
          'BARGE_IN ONSET_MISSING route=$_activeRoute '
          'peak_rms=${_windowPeakRms.toStringAsFixed(0)} '
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
    if (_useSoftwareAec && !voiceRecent) {
      // Energy ghost gate — ONLY meaningful while the software AEC3 is
      // wired: residual echo reaching Gemini's VAD with no local voice
      // evidence. With the gate's premise gone (platform AEC on speaker,
      // earphones can't hear playout at all), rejecting here swallowed
      // REAL barge-ins (measured 30 Aug earphone: GHOST_REJECT on genuine
      // speech → duck instead of flush → answer played to the end).
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
      // Hard flush (instant silence) unless the software AEC3 pipeline is
      // wired — releasing the track mid-session invalidates THAT model.
      // Platform AEC (the primary path) has no client-side model to lose.
      stoppedAt = await _audioPlayback.flush(hard: !_useSoftwareAec);
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
    // The on-screen barge-in panel is a PER-SESSION view: stale lines from
    // the previous session read as current-session evidence. (The durable
    // sirious_bargein.log file keeps appending across sessions by design —
    // adb-pull debugging lifeline.)
    _bargeInLog.clear();
    _currentUserText = '';
    _currentAssistantText = '';
    _currentTurnStartedAt = null;
    latency.sessionConnectedAt = null;
    latency.firstMicChunkAt = null;
    latency.resetForTurn();
    // Step 4.5: a NEW session starts with clean adaptive state (the prior
    // session's room/residual must not seed this one; reconnects within a
    // session deliberately keep it).
    _recentRms.clear();
    _onsetNoiseFloor = 0;
    _playbackResidualWindow.clear();
    _playbackResidual = 0;
    _prevOnsetDecision = AdaptiveVadPolicy.playbackLowerClamp;
    _prevChunkAboveHold = false;
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

    // Stage C (1): capture profile follows the AUDIO ROUTE. Earphones →
    // nearTalk (raw mic; no echo possible); builtin speaker → the
    // platform-AEC profile (voiceCommunication + InCommunication, probed
    // 30 Aug). The software AEC3 pipeline stays unwired (backup — see
    // _useSoftwareAec).
    _activeRoute = CaptureRoutePolicy.classifyRoute(
      await AudioRouteWatcher.instance.detectRoute(),
    );
    final routeProfile = duckCapture
        ? CaptureProfile.nearTalk // ambient C2: capture ducked during playback
        : CaptureRoutePolicy.profileForRoute(_activeRoute);
    unawaited(
      _logToFile(
        'ROUTE start route=$_activeRoute profile=$routeProfile '
        'gain=${CaptureRoutePolicy.gainForProfile(routeProfile).toStringAsFixed(1)}x '
        'duck=$duckCapture',
      ),
    );
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
        vadManual: _manualVadActive,
      );
      await _audioCapture.start(
        profile: routeProfile,
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
    _vadClose(reason: 'session_end');
    _levelStampDone = false;
    _levelSumSq = 0;
    _levelChunks = 0;
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

    // Gain validation stamp (step 2 follow-up): average the first ~2 s of
    // chunks and log the level ONCE per session. With gain g, pre-gain RMS
    // = value/gain — the first instrumented session settles whether the 4x
    // is end-to-end by numbers instead of inference (30 Aug night session
    // left it open: peaks 95-135 sat where pre-gain numbers were).
    if (!_levelStampDone && _manualVadActive) {
      final rms = _rms(chunk);
      _levelSumSq += rms * rms;
      _levelChunks++;
      if (_levelChunks >= 20) {
        _levelStampDone = true;
        final ambient = math.sqrt(_levelSumSq / _levelChunks);
        final gain = _audioCapture.activeGain;
        unawaited(
          _logToFile(
            'LEVEL ambient_rms=${ambient.toStringAsFixed(1)} '
            'gain=${gain.toStringAsFixed(1)}x '
            'pre_gain_rms=${(ambient / gain).toStringAsFixed(1)}',
          ),
        );
      }
    }

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
    // With manual VAD the SAME energy evaluation also runs in LISTENING
    // phase (the first user utterance of a turn arrives there) and its
    // sustained result drives the VAD window.
    final manualVad = _manualVadActive;
    bool speechHeld = false;
    double speechRms = 0;
    if (_phase == SessionPhase.playing ||
        _phase == SessionPhase.responding ||
        _phase == SessionPhase.interrupting) {
      final rms = _rms(chunk);
      if (rms > _windowPeakRms) {
        _windowPeakRms = rms;
      }

      // Near-silence floor update — MANUAL VAD: the ambient window is NOT
      // fed during playback. The room level was measured in LISTENING and
      // must stay frozen here: the answer's own residual (84–168 in loud
      // rooms) sits below the admission bar and would ratchet the "ambient"
      // up to itself, pushing the room term to 336+ (eats real barge-ins
      // 255–305). Echo-level changes during playback are tracked by the
      // residual term instead. Server-VAD paths keep the legacy update.
      if (!manualVad && rms < _ambientAdmissionFloor * 2) {
        _recentRms.add(rms);
        if (_recentRms.length > _noiseWindowChunks) {
          _recentRms.removeAt(0);
        }
      }
      _onsetNoiseFloor = _ambientFloor;
      latency.lastBargeInNoiseFloor = _onsetNoiseFloor;
      // Step 4.5: feed the residual window (EVERY playback chunk — the p25
      // absorbs bursts and near-silence tails) and refresh the measured
      // residual that the adaptive playback onset clears.
      _playbackResidualWindow.add(rms);
      if (_playbackResidualWindow.length >
          AdaptiveVadPolicy.residualWindowChunks) {
        _playbackResidualWindow.removeAt(0);
      }
      _playbackResidual = AdaptiveVadPolicy.residual(_playbackResidualWindow);

      // Stage B (B3): while Sirious plays, the threshold must also clear the
      // residual echo floor at the current volume (measured post-AEC by the
      // pipeline, ~1s window) and a higher hard floor (echo bursts measured
      // 266-521 RMS while real speech at speaker distance is 1000+). In
      // listening phase residual is 0 and the normal floor applies.
      final residual = _aecPipeline?.residualFloor ?? 0;
      // Step 4.5 (manual VAD): ADAPTIVE playback onset — room term
      // (ambient×4) + measured-residual term (p25×2), floored at 150.
      // The fixed 450/150 floors were both room-calibrated by definition
      // and failed on the opposite rooms (31 Aug). Server-VAD paths keep
      // the legacy fixed-floor math.
      final threshold = manualVad
          ? _adaptiveOnset
          : math.max(
              math.max(
                _onsetNoiseFloor * _onsetRiseFactor,
                residual * _residualRiseFactor,
              ),
              _onsetPlaybackHardFloor,
            );
      _prevOnsetDecision = threshold;

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
      final prevLoud = _wasLoud;
      final prevAboveHold = _prevChunkAboveHold;
      final sustained = aec == null || !aec.farEndRecently || prevLoud;
      if (rms > threshold) {
        if (sustained) {
          if (latency.bargeInOnsetAt == null) {
            latency.bargeInOnsetAt = DateTime.now();
            unawaited(
              _logToFile(
                'ONSET route=$_activeRoute rms=${rms.toStringAsFixed(0)} '
                'thr=${threshold.toStringAsFixed(0)} floor=${_onsetNoiseFloor.toStringAsFixed(0)} '
                'res=${residual.toStringAsFixed(0)} '
                'pres=${_playbackResidual.toStringAsFixed(0)} sustained=true',
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
      final nowLoud = rms > threshold;
      _wasLoud = nowLoud;
      _prevChunkAboveHold = rms > _vadHoldLevel;
      // Manual VAD: playback windows open on SUSTAINED voice only — chunk 1
      // above onset AND chunk 2 above hold (see _prevChunkAboveHold). The
      // single-burst echo transient that killed answers in the 17:00 loop
      // never gets a window now; the ONSET line still logs the crossing.
      speechHeld = _manualVadActive
          ? (nowLoud && prevAboveHold)
          : nowLoud && sustained;
      speechRms = rms;
    }

    // ── Step 4: manual client VAD (speaker route) ─────────────────────────
    // In LISTENING the energy block above does not run; evaluate the same
    // onset logic here so the first utterance of a turn opens the window.
    // (Playback-phase chunks already evaluated speechHeld above.)
    bool speech = speechHeld;
    if (manualVad && _phase == SessionPhase.listening) {
      final rms = _rms(chunk);
      speechRms = rms;
      // Adaptive floor in LISTENING (the playback-phase block above never
      // ran here, so the floor stayed 0 — VAD_OPEN rms=… floor=0 in the
      // 31 Aug 00:00 log). Same near-silence discipline, FIXED admission
      // bar (speech-excluding; the adaptive floor itself must not decide
      // admission — self-referential when speech is this quiet).
      if (rms < _ambientAdmissionFloor * 2) {
        _recentRms.add(rms);
        if (_recentRms.length > _noiseWindowChunks) {
          _recentRms.removeAt(0);
        }
        _onsetNoiseFloor = _ambientFloor;
        latency.lastBargeInNoiseFloor = _onsetNoiseFloor;
      }
      // Step 4.5: adaptive onset, 80–150 band. The fixed 240 ate quiet
      // speech (166–249 measured, 01:33 session — VAD_GATE while the user
      // talked); the band's ceiling keeps breath/creak sounds (80–150 in
      // quiet rooms) from being entirely free, and any real speech ≥166
      // always crosses. Windows that open on non-speech close themselves
      // within ~1 s (8 silent chunks) — bounded cost, sensitivity wins.
      final threshold = _adaptiveOnset;
      _prevOnsetDecision = threshold;
      speech = rms > threshold;
    }

    if (manualVad) {
      _vadTick(speech: speech, rms: speechRms);
      if (speech && _vadOpenedAt == null) {
        _vadOpen(speechRms);
        // CLIENT-DRIVEN BARGE-IN: a sustained-voice window opening while
        // Sirious talks IS the interruption — the server's `interrupted`
        // only confirms it later (it can no longer invent one). Flush now:
        // ~1 server round-trip faster than the old path, and immune to
        // residue-driven phantoms.
        if (_phase == SessionPhase.playing ||
            _phase == SessionPhase.responding ||
            _phase == SessionPhase.interrupting) {
          unawaited(_handleInterruption(DateTime.now()));
        }
      }
      // Stream gate: outside an open window NOTHING reaches the server —
      // the residue (assistant-echo tails, ambient noise, our own residual)
      // can no longer decide anything server-side. Inside the window audio
      // flows normally (platform AEC has already removed the echo).
      if (_vadOpenedAt == null) {
        return;
      }
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

  /// Stage C (1): the audio output route changed → re-classify and, on a
  /// route-CLASS change (earphones ↔ speaker), restart capture on the new
  /// profile MID-SESSION. Within-class device changes (BT↔wired, speaker
  /// swaps) keep the current profile — both profiles ride whatever the OS
  /// hands them. The software AEC3 pipeline (when re-enabled) is rebuilt
  /// with the capture: a route change shifts the echo path, so its delay
  /// estimator must re-lock on the new one.
  Future<void> _onAudioRouteChanged() async {
    final route = CaptureRoutePolicy.classifyRoute(
      await AudioRouteWatcher.instance.detectRoute(),
    );
    if (route == _activeRoute) {
      unawaited(
        _logToFile('ROUTE event → same class ($_activeRoute) — no-op'),
      );
      return;
    }
    if (!_phase.isActive) {
      // No live session: remember it; the next startSession() picks the
      // fresh profile anyway.
      _activeRoute = route;
      return;
    }
    final now = DateTime.now();
    if (now.difference(_lastRouteReinitAt).inMilliseconds <
        _routeReinitCooldownMs) {
      return; // coalesce duplicate native callbacks
    }
    _lastRouteReinitAt = now;
    _routeReinitCount++;
    final oldRoute = _activeRoute;
    _activeRoute = route;

    try {
      await _audioCapture.stop();
    } catch (_) {}
    try {
      await _audioCapture.start(
        profile: CaptureRoutePolicy.profileForRoute(route),
      );
    } catch (error) {
      _errorMessage = 'Capture restart failed after route change: $error';
      unawaited(_logToFile('ROUTE restart FAILED: $error'));
      notifyListeners();
      return;
    }

    if (_useSoftwareAec) {
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
      } catch (err) {
        _aecPipeline = null;
        unawaited(_logToFile('AEC route re-init failed: $err'));
      }
    }

    // The onset window re-learns on the NEW capture path: stale noise-floor
    // samples from the old path (10x level difference) would deafen or
    // saturate the threshold for up to the full 2s window. Step 4.5: this
    // must ALSO clear the adaptive windows — cross-turn persistence is only
    // safe on the SAME capture path; a route restart changes the path.
    _endOnsetWindow();
    _recentRms.clear();
    _onsetNoiseFloor = 0;
    _playbackResidualWindow.clear();
    _playbackResidual = 0;
    // Manual VAD: the window rides the capture path too — a route restart
    // closes any open window on the old profile (its activityEnd still
    // signals the server).
    _vadClose(reason: 'route_restart');

    final line =
        'ROUTE #$_routeReinitCount: $oldRoute → $route — capture restarted '
        '(${CaptureRoutePolicy.profileForRoute(route)}, '
        'gain=${CaptureRoutePolicy.gainForProfile(CaptureRoutePolicy.profileForRoute(route)).toStringAsFixed(1)}x, '
        'floors follow route)';
    _pushBargeInLine(line);
    unawaited(_logToFile(line));
    notifyListeners();
  }

  Future<void> _onJsonEvent(Map<String, dynamic> event) async {
    final type = event['type'] as String?;
    final manualVad = _manualVadActive;
    debugPrint('WS_EVENT: $type');
    unawaited(_logToFile('EVENT $type ${event['text'] ?? ''}'));

    switch (type) {
      case 'session_started':
        _sessionId = event['session_id'] as String?;
        latency.sessionConnectedAt = DateTime.now();
        final resumed = event['resumed'] == true;
        final vadMode = event['vad_mode'] as String?;
        if (vadMode != null) {
          unawaited(_logToFile('VAD mode=$vadMode (client wanted $_manualVadActive)'));
        }
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

        // Step 4 (manual VAD): NO windowless-drop here. An earlier build
        // dropped transcripts arriving with no open VAD window — wrong under
        // manual VAD: the stream gate makes residue impossible, and the
        // transcript legitimately arrives ~200-300 ms AFTER activity_end
        // (server-side ASR finalizes after speech ends). Dropping it ate
        // every user turn (31 Aug 01:09 report: "I don't see my turns").

        // Post-turn echo guard: the assistant's own tail can keep arriving
        // arriving through the AEC residual after playback ends; a "user"
        // transcript repeating just-spoken assistant words within the guard
        // window is echo — do not open a user turn with it.
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
        // Step 4 (manual VAD): with automatic VAD off the server can no
        // longer INVENT an interruption — `interrupted` means the model's
        // generation was cancelled by OUR activityEnd (docs: an activityEnd
        // marks the interruption). It is a CONFIRM of a client-initiated
        // interruption, not a trigger; the flush already ran client-side
        // when the window opened during playback. _handleInterruption is
        // idempotent per turn (_interruptionHandled) and this handler keeps
        // the legacy measurement path for non-manual modes.
        if (manualVad) {
          unawaited(
            _logToFile(
              'VAD_SERVER_INTERRUPTED route=$_activeRoute '
              'windowOpen=${_vadOpenedAt != null} (client-driven confirm)',
            ),
          );
        }
        await _handleInterruption(DateTime.now());
        break;

      case 'turn_complete':
        // Fix 30 Aug: if an ACCEPTED interruption is mid-flight (its flush
        // await suspended this handler chain), turn_complete arriving
        // mid-flight must NOT commit the turn as non-interrupted and wipe
        // the barge-in measurements before _handleInterruption's
        // continuation does it — that race ate the on-screen (interrupted)
        // tag and the latency numbers (log: ONSET_MISSING peak_rms=0).
        if (_interruptionHandled) {
          // The interrupted handler's continuation commits (interrupted:
          // true) and resets the onset window; just close the detector's
          // turn bookkeeping.
          _ghostDetector.markTurnComplete();
          break;
        }
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
      await _webSocketClient.connect(
        clientSessionId: _clientSessionId,
        vadManual: _manualVadActive,
      );
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
    // Step 4.5 (manual VAD): the AMBIENT window persists across turns —
    // the room does not change between sentences, and wiping it reset the
    // floor to 0 every turn (31 Aug loud-room failure: each answer started
    // blind on the fixed 150 floor). A real room change re-learns within
    // the 2 s window (speech-excluding admission). The measured playback
    // residual persists too — it seeds the next answer's adaptive floor.
    // Server-VAD paths keep the legacy full reset.
    if (!_manualVadActive) {
      _recentRms.clear();
      _onsetNoiseFloor = 0;
    }
    _windowPeakRms = 0;
    _lastVoiceLikeAt = null; // ghost gate: stale voice evidence must not count
    _wasLoud = false; // sustain gate: a stale "was loud" must not carry over
    _prevChunkAboveHold = false; // step 4.6: same discipline for the open gate
    _interruptionHandled = false;
    _prevOnsetDecision = AdaptiveVadPolicy.playbackLowerClamp;
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
