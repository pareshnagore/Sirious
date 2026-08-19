import 'package:flutter/material.dart';

import '../../models/session_phase.dart';

class StatusIndicator extends StatelessWidget {
  const StatusIndicator({super.key, required this.phase, this.sessionId});

  final SessionPhase phase;
  final String? sessionId;

  Color _colorForPhase(SessionPhase phase) {
    switch (phase) {
      case SessionPhase.listening:
        return Colors.green;
      case SessionPhase.connecting:
      case SessionPhase.reconnecting:
      case SessionPhase.responding:
      case SessionPhase.playing:
        return Colors.blue;
      case SessionPhase.interrupting:
        return Colors.orange;
      case SessionPhase.error:
        return Colors.red;
      case SessionPhase.idle:
      case SessionPhase.ending:
        return Colors.grey;
    }
  }

  @override
  Widget build(BuildContext context) {
    final color = _colorForPhase(phase);

    return Column(
      children: [
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Container(
              width: 12,
              height: 12,
              decoration: BoxDecoration(color: color, shape: BoxShape.circle),
            ),
            const SizedBox(width: 8),
            Text(phase.label, style: Theme.of(context).textTheme.titleMedium),
          ],
        ),
        if (sessionId != null) ...[
          const SizedBox(height: 4),
          Text(
            'Session ${sessionId!.substring(0, 8)}…',
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ],
      ],
    );
  }
}
