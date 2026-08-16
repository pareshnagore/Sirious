import 'package:flutter/material.dart';

import '../../models/transcript_turn.dart';

class TranscriptPanel extends StatelessWidget {
  const TranscriptPanel({
    super.key,
    required this.turns,
    required this.currentUserText,
    required this.currentAssistantText,
  });

  final List<TranscriptTurn> turns;
  final String currentUserText;
  final String currentAssistantText;

  @override
  Widget build(BuildContext context) {
    final textTheme = Theme.of(context).textTheme;

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        for (final turn in turns) ...[
          if (turn.userText.isNotEmpty)
            _Bubble(
              label: 'You',
              text: turn.userText,
              color: Theme.of(context).colorScheme.primaryContainer,
            ),
          if (turn.assistantText.isNotEmpty)
            _Bubble(
              label: turn.interrupted ? 'Sirious (interrupted)' : 'Sirious',
              text: turn.assistantText,
              color: Theme.of(context).colorScheme.secondaryContainer,
            ),
          const SizedBox(height: 12),
        ],
        if (currentUserText.isNotEmpty)
          _Bubble(
            label: 'You',
            text: currentUserText,
            color: Theme.of(context).colorScheme.primaryContainer,
            live: true,
          ),
        if (currentAssistantText.isNotEmpty)
          _Bubble(
            label: 'Sirious',
            text: currentAssistantText,
            color: Theme.of(context).colorScheme.secondaryContainer,
            live: true,
          ),
        if (turns.isEmpty &&
            currentUserText.isEmpty &&
            currentAssistantText.isEmpty)
          Center(
            child: Padding(
              padding: const EdgeInsets.only(top: 48),
              child: Text(
                'Start a session and speak naturally.',
                style: textTheme.bodyLarge?.copyWith(
                  color: Theme.of(context).colorScheme.outline,
                ),
              ),
            ),
          ),
      ],
    );
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble({
    required this.label,
    required this.text,
    required this.color,
    this.live = false,
  });

  final String label;
  final String text;
  final Color color;
  final bool live;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: color,
        borderRadius: BorderRadius.circular(12),
        border: live
            ? Border.all(
                color: Theme.of(context).colorScheme.primary,
                width: 1,
              )
            : null,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: Theme.of(context).textTheme.labelMedium,
          ),
          const SizedBox(height: 4),
          Text(text),
        ],
      ),
    );
  }
}
