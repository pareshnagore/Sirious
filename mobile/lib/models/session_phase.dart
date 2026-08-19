enum SessionPhase {
  idle,
  connecting,
  reconnecting,
  listening,
  responding,
  playing,
  interrupting,
  ending,
  error,
}

extension SessionPhaseLabel on SessionPhase {
  String get label {
    switch (this) {
      case SessionPhase.idle:
        return 'Idle';
      case SessionPhase.connecting:
        return 'Connecting…';
      case SessionPhase.reconnecting:
        return 'Reconnecting…';
      case SessionPhase.listening:
        return 'Listening';
      case SessionPhase.responding:
        return 'Responding';
      case SessionPhase.playing:
        return 'Playing';
      case SessionPhase.interrupting:
        return 'Interrupting';
      case SessionPhase.ending:
        return 'Ending…';
      case SessionPhase.error:
        return 'Error';
    }
  }

  bool get isActive {
    switch (this) {
      case SessionPhase.connecting:
      case SessionPhase.reconnecting:
      case SessionPhase.listening:
      case SessionPhase.responding:
      case SessionPhase.playing:
      case SessionPhase.interrupting:
      case SessionPhase.ending:
        return true;
      case SessionPhase.idle:
      case SessionPhase.error:
        return false;
    }
  }
}
