import 'package:flutter/material.dart';

import '../models/session_history.dart';
import '../services/history_api.dart';

/// Phase 2: full transcript of one past session.
class SessionDetailScreen extends StatefulWidget {
  const SessionDetailScreen({super.key, required this.sessionId});

  final String sessionId;

  @override
  State<SessionDetailScreen> createState() => _SessionDetailScreenState();
}

class _SessionDetailScreenState extends State<SessionDetailScreen> {
  final HistoryApi _api = HistoryApi();
  late Future<SessionDetail> _future;

  @override
  void initState() {
    super.initState();
    _future = _api.getSession(widget.sessionId);
  }

  static String _fmtTime(String? iso) {
    if (iso == null) {
      return '';
    }
    final d = DateTime.tryParse(iso)?.toLocal();
    if (d == null) {
      return '';
    }
    return '${d.hour.toString().padLeft(2, '0')}:${d.minute.toString().padLeft(2, '0')}:${d.second.toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Transcript')),
      body: FutureBuilder<SessionDetail>(
        future: _future,
        builder: (context, snap) {
          if (snap.connectionState != ConnectionState.done) {
            return const Center(child: CircularProgressIndicator());
          }
          if (snap.hasError) {
            return Center(child: Text('${snap.error}'));
          }
          final s = snap.data!;
          return Column(
            children: [
              Padding(
                padding: const EdgeInsets.all(12),
                child: Text(
                  [
                    if (s.title != null) s.title!,
                    if (s.isAmbient) 'ambient',
                    '${s.turns.length} turns',
                    if (s.durationS != null)
                      '${s.durationS!.toStringAsFixed(0)}s',
                    if (s.resumeCount > 0)
                      'resumed ×${s.resumeCount}',
                  ].join(' · '),
                  style: Theme.of(context).textTheme.bodySmall,
                  textAlign: TextAlign.center,
                ),
              ),
              const Divider(height: 1),
              Expanded(
                child: ListView.builder(
                  padding: const EdgeInsets.symmetric(
                      horizontal: 16, vertical: 8),
                  itemCount: s.turns.length,
                  itemBuilder: (context, i) {
                    final t = s.turns[i];
                    // Ambient sessions render diarized turns; voice sessions
                    // keep the You/Sirious pair layout.
                    if (s.isAmbient || t.isAmbient) {
                      return Padding(
                        padding: const EdgeInsets.only(bottom: 12),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              'S${t.speaker ?? '?'}',
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
                            Text(t.ambientText),
                          ],
                        ),
                      );
                    }
                    return Padding(
                      padding: const EdgeInsets.only(bottom: 14),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(
                            children: [
                              Text(
                                'You',
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
                              const Spacer(),
                              Text(
                                _fmtTime(t.startedAt),
                                style:
                                    Theme.of(context).textTheme.labelSmall,
                              ),
                            ],
                          ),
                          if (t.userText.isNotEmpty)
                            Text(t.userText),
                          const SizedBox(height: 6),
                          Text(
                            'Sirious',
                            style: Theme.of(context)
                                .textTheme
                                .labelSmall
                                ?.copyWith(
                                  color: Theme.of(context)
                                      .colorScheme
                                      .secondary,
                                  fontWeight: FontWeight.bold,
                                ),
                          ),
                          if (t.assistantText.isNotEmpty)
                            Text(t.assistantText),
                          if (t.interrupted)
                            Padding(
                              padding: const EdgeInsets.only(top: 2),
                              child: Text(
                                '· interrupted',
                                style: Theme.of(context)
                                    .textTheme
                                    .labelSmall
                                    ?.copyWith(fontStyle: FontStyle.italic),
                              ),
                            ),
                        ],
                      ),
                    );
                  },
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}
