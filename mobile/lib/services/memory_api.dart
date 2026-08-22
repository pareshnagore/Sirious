import 'dart:convert';

import 'package:http/http.dart' as http;

import '../config/app_config.dart';
import 'auth_service.dart';
import 'history_api.dart' show HistoryApiException, HistoryAuthException;

/// Client for the Phase 3 memory REST API.
/// GET /memories (list or ?q= semantic search) and DELETE /memories/{id}.
class MemoryApi {
  MemoryApi({http.Client? client, AuthService? authService})
      : _client = client ?? http.Client(),
        _auth = authService ?? AuthService();

  final http.Client _client;
  final AuthService _auth;

  Future<Map<String, String>> _headers() async {
    final token = await _auth.getToken();
    return {
      'Authorization': 'Bearer $token',
      'Content-Type': 'application/json',
    };
  }

  /// Newest-first when [query] is null; cosine-ranked hits otherwise.
  Future<List<MemoryItem>> listMemories({String? query, int limit = 200}) async {
    if (await _auth.getToken() == null) return [];
    final uri = Uri.parse('${AppConfig.apiBase}/memories').replace(
      queryParameters: {
        'limit': '$limit',
        if (query != null && query.trim().isNotEmpty) 'q': query.trim(),
      },
    );
    final resp = await _client.get(uri, headers: await _headers());
    if (resp.statusCode == 401) throw HistoryAuthException();
    if (resp.statusCode != 200) {
      throw HistoryApiException('memories failed: HTTP ${resp.statusCode}');
    }
    final body = jsonDecode(resp.body) as Map<String, dynamic>;
    final items = (body['memories'] as List? ?? const [])
        .map((m) => MemoryItem.fromJson(m as Map<String, dynamic>))
        .toList();
    return items;
  }

  /// Soft-delete a wrong memory. Returns false on 404.
  Future<bool> deleteMemory(String id) async {
    final uri = Uri.parse('${AppConfig.apiBase}/memories/$id');
    final resp = await _client.delete(uri, headers: await _headers());
    if (resp.statusCode == 401) throw HistoryAuthException();
    if (resp.statusCode == 404) return false;
    if (resp.statusCode != 200) {
      throw HistoryApiException('delete failed: HTTP ${resp.statusCode}');
    }
    return true;
  }
}

/// One extracted long-term memory.
class MemoryItem {
  MemoryItem({
    required this.id,
    required this.type,
    required this.text,
    this.topics = const [],
    this.entities = const [],
    this.timesSeen = 1,
    this.lastSeenAt,
    this.provenance = const [],
  });

  factory MemoryItem.fromJson(Map<String, dynamic> j) => MemoryItem(
        id: j['id'] as String,
        type: (j['type'] ?? 'semantic') as String,
        text: (j['text'] ?? '') as String,
        topics: (j['topics'] as List? ?? const []).cast<String>(),
        entities: (j['entities'] as List? ?? const []).cast<String>(),
        timesSeen: (j['times_seen'] ?? 1) as int,
        lastSeenAt: j['last_seen_at'] as String?,
        provenance: ((j['provenance'] as List? ?? const [])
                .whereType<Map<String, dynamic>>()
                .map((p) => MemoryProvenance.fromJson(p))
                .toList())
            .cast<MemoryProvenance>(),
      );

  final String id;
  final String type; // episodic | semantic | entity | task
  final String text;
  final List<String> topics;
  final List<String> entities;
  final int timesSeen;
  final String? lastSeenAt;
  final List<MemoryProvenance> provenance;
}

class MemoryProvenance {
  MemoryProvenance({
    this.sessionRef,
    this.startedAt,
    this.title,
  });

  factory MemoryProvenance.fromJson(Map<String, dynamic> j) => MemoryProvenance(
        sessionRef: j['session_ref'] as String?,
        startedAt: j['started_at'] as String?,
        title: j['title'] as String?,
      );

  final String? sessionRef;
  final String? startedAt;
  final String? title;
}
