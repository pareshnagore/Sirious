import 'package:flutter/material.dart';

import '../services/auth_service.dart';
import '../services/history_api.dart';
import '../models/session_history.dart';
import 'session_detail_screen.dart';

/// Phase 2: list of past Sirious conversations, newest first.
class HistoryScreen extends StatefulWidget {
  const HistoryScreen({super.key});

  @override
  State<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends State<HistoryScreen> {
  final HistoryApi _api = HistoryApi();
  List<SessionSummary>? _sessions;   // kept locally so deletes can remove rows
  late Future<List<SessionSummary>> _future;
  String? _token;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _future = _api.listSessions();
    });
    _future.then((s) {
      if (mounted) setState(() => _sessions = s);
    });
    _token = await AuthService().getToken();
    if (mounted) {
      setState(() {});
    }
  }

  Future<void> _confirmDelete(SessionSummary s) async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete conversation?'),
        content: Text(
          '"${s.title ?? '(no speech captured)'}" will be removed along with '
          'the memories it came from.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (yes != true) return;
    try {
      final stats = await _api.deleteSession(s.id);
      if (!mounted) return;
      setState(() {
        _sessions = (_sessions ?? []).where((x) => x.id != s.id).toList();
      });
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            'Conversation deleted · memories updated ${stats['updated']}, '
            'removed ${stats['deleted']}',
          ),
        ),
      );
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Delete failed: $e')),
      );
      _load();
    }
  }

  Future<void> _editToken(BuildContext context) async {
    final controller = TextEditingController(text: _token);
    final saved = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('API token'),
        content: TextField(
          controller: controller,
          obscureText: true,
          decoration: const InputDecoration(
            hintText: 'Paste the SIRIOUS_AUTH_TOKEN value',
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Save'),
          ),
        ],
      ),
    );
    if (saved == true && controller.text.trim().isNotEmpty) {
      await AuthService().saveToken(controller.text);
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Token saved')),
        );
      }
      _load();
    }
  }

  static String _fmtDate(String? iso) {
    if (iso == null) {
      return '';
    }
    final d = DateTime.tryParse(iso)?.toLocal();
    if (d == null) {
      return '';
    }
    return '${d.day}/${d.month} ${d.hour.toString().padLeft(2, '0')}:${d.minute.toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('History'),
        actions: [
          IconButton(
            icon: const Icon(Icons.key),
            tooltip: 'API token',
            onPressed: () => _editToken(context),
          ),
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: _load,
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async => _load(),
        child: FutureBuilder<List<SessionSummary>>(
          future: _future,
          builder: (context, snap) {
            if (snap.connectionState != ConnectionState.done) {
              return const Center(child: CircularProgressIndicator());
            }
            if (snap.hasError) {
              return ListView(
                children: [
                  Padding(
                    padding: const EdgeInsets.all(24),
                    child: Column(
                      children: [
                        Icon(Icons.cloud_off,
                            size: 40,
                            color: Theme.of(context).colorScheme.error),
                        const SizedBox(height: 12),
                        Text('${snap.error}',
                            textAlign: TextAlign.center),
                        const SizedBox(height: 12),
                        OutlinedButton.icon(
                          onPressed: _load,
                          icon: const Icon(Icons.refresh),
                          label: const Text('Retry'),
                        ),
                      ],
                    ),
                  ),
                ],
              );
            }
            final sessions = snap.data ?? [];
            if (sessions.isEmpty) {
              return ListView(
                children: const [
                  Padding(
                    padding: EdgeInsets.all(32),
                    child: Center(
                      child: Text(
                        'No sessions yet.\nHave a voice conversation first — '
                        'it will appear here.',
                        textAlign: TextAlign.center,
                      ),
                    ),
                  ),
                ],
              );
            }
            return ListView.separated(
              itemCount: sessions.length,
              separatorBuilder: (_, _) => const Divider(height: 1),
              itemBuilder: (context, i) {
                final s = sessions[i];
                return Dismissible(
                  key: ValueKey(s.id),
                  direction: DismissDirection.endToStart,
                  background: Container(
                    color: Theme.of(context).colorScheme.errorContainer,
                    alignment: Alignment.centerRight,
                    padding: const EdgeInsets.only(right: 24),
                    child: Icon(Icons.delete,
                        color: Theme.of(context).colorScheme.onErrorContainer),
                  ),
                  confirmDismiss: (_) => _confirmDelete(s).then((_) => false),
                  child: ListTile(
                    title: Text(
                      s.title ?? '(no speech captured)',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    subtitle: Text(
                      [
                        _fmtDate(s.startedAt),
                        if (s.durationS != null)
                          '${s.durationS!.toStringAsFixed(0)}s',
                        '${s.turnCount ?? 0} turns',
                      ].join(' · '),
                    ),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => Navigator.push(
                      context,
                      MaterialPageRoute<void>(
                        builder: (_) =>
                            SessionDetailScreen(sessionId: s.id),
                      ),
                    ),
                  ),
                );
              },
            );
          },
        ),
      ),
    );
  }
}
