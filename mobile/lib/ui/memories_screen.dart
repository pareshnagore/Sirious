import 'package:flutter/material.dart';

import '../services/auth_service.dart';
import '../services/memory_api.dart';
import 'session_detail_screen.dart';

/// Phase 3: what Sirious remembers — view, search, delete.
class MemoriesScreen extends StatefulWidget {
  const MemoriesScreen({super.key});

  @override
  State<MemoriesScreen> createState() => _MemoriesScreenState();
}

class _MemoriesScreenState extends State<MemoriesScreen> {
  final MemoryApi _api = MemoryApi();
  List<MemoryItem> _items = [];
  String? _error;
  bool _loading = true;
  String? _token;
  final _searchCtrl = TextEditingController();

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _searchCtrl.dispose();
    super.dispose();
  }

  Future<void> _load({String? query}) async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final items = await _api.listMemories(query: query);
      if (!mounted) return;
      setState(() {
        _items = items;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = '$e';
        _loading = false;
      });
    }
    final token = await AuthService().getToken();
    if (mounted) setState(() => _token = token);
  }

  Future<void> _confirmDelete(MemoryItem m) async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Forget this memory?'),
        content: Text(m.text),
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
      await _api.deleteMemory(m.id);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Memory deleted')),
      );
      _load();
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Delete failed: $e')),
      );
    }
  }

  static IconData _iconFor(String type) {
    switch (type) {
      case 'episodic':
        return Icons.history_edu;
      case 'task':
        return Icons.check_circle_outline;
      case 'entity':
        return Icons.tag;
      default:
        return Icons.lightbulb_outline;
    }
  }

  /// 'You discussed this on 22 Aug' style line from provenance.
  String _provenanceLine(MemoryItem m) {
    final p = m.provenance.isEmpty ? null : m.provenance.last;
    if (p == null || p.sessionRef == null) return '';
    final d = DateTime.tryParse(p.startedAt ?? '')?.toLocal();
    final date = d == null
        ? ''
        : '${d.day}/${d.month}/${d.year} · ';
    return '$date${m.timesSeen > 1 ? 'came up ${m.timesSeen}× · ' : ''}'
        'tap for conversation';
  }

  @override
  Widget build(BuildContext context) {
    final noToken = _token == null;
    return Scaffold(
      appBar: AppBar(
        title: const Text('What Sirious remembers'),
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
            child: TextField(
              controller: _searchCtrl,
              textInputAction: TextInputAction.search,
              onSubmitted: (q) => _load(query: q),
              decoration: InputDecoration(
                hintText: '"did we talk about birds?"',
                prefixIcon: const Icon(Icons.search),
                suffixIcon: IconButton(
                  icon: const Icon(Icons.close),
                  onPressed: () {
                    _searchCtrl.clear();
                    _load();
                  },
                ),
              ),
            ),
          ),
          Expanded(
            child: _buildBody(context, noToken),
          ),
        ],
      ),
    );
  }

  Widget _buildBody(BuildContext context, bool noToken) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return ListView(
        children: [
          Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              children: [
                Icon(Icons.cloud_off,
                    size: 40, color: Theme.of(context).colorScheme.error),
                const SizedBox(height: 12),
                Text(_error!, textAlign: TextAlign.center),
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
    if (noToken) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(32),
          child: Text(
            'Set your API token in History (key icon) to see memories.',
            textAlign: TextAlign.center,
          ),
        ),
      );
    }
    if (_items.isEmpty) {
      return ListView(
        children: const [
          Padding(
            padding: EdgeInsets.all(32),
            child: Center(
              child: Text(
                'Nothing remembered yet.\nHave a voice conversation first — '
                'memories are extracted after each session ends.',
                textAlign: TextAlign.center,
              ),
            ),
          ),
        ],
      );
    }
    return RefreshIndicator(
      onRefresh: () async => _load(),
      child: ListView.separated(
        itemCount: _items.length,
        separatorBuilder: (_, _) => const Divider(height: 1),
        itemBuilder: (context, i) {
          final m = _items[i];
          return ListTile(
            leading: Icon(_iconFor(m.type)),
            title: Text(m.text),
            subtitle: Text(_provenanceLine(m)),
            isThreeLine: false,
            trailing: IconButton(
              icon: const Icon(Icons.delete_outline),
              tooltip: 'Delete memory',
              onPressed: () => _confirmDelete(m),
            ),
            onTap: () {
              final ref = m.provenance.isEmpty ? null : m.provenance.last.sessionRef;
              if (ref == null) return;
              Navigator.push(
                context,
                MaterialPageRoute<void>(
                  builder: (_) => SessionDetailScreen(sessionId: ref),
                ),
              );
            },
          );
        },
      ),
    );
  }
}
