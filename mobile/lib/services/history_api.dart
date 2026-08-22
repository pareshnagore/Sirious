import 'dart:convert';

import 'package:http/http.dart' as http;

import '../config/app_config.dart';
import '../models/session_history.dart';
import 'auth_service.dart';

/// Client for the Phase 2 session-history REST API.
/// GET /sessions and GET /sessions/{id}, bearer-token auth.
class HistoryApi {
  HistoryApi({http.Client? client, AuthService? authService})
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

  /// Returns sessions newest-first; empty list when no token yet.
  Future<List<SessionSummary>> listSessions({int limit = 50}) async {
    if (await _auth.getToken() == null) {
      return [];
    }
    final uri =
        Uri.parse('${AppConfig.apiBase}/sessions').replace(queryParameters: {
      'limit': '$limit',
    });
    final resp = await _client.get(uri, headers: await _headers());
    if (resp.statusCode == 401) {
      throw HistoryAuthException();
    }
    if (resp.statusCode != 200) {
      throw HistoryApiException('list failed: HTTP ${resp.statusCode}');
    }
    final body = jsonDecode(resp.body) as Map<String, dynamic>;
    final items = (body['sessions'] as List? ?? const [])
        .map((s) => SessionSummary.fromJson(s as Map<String, dynamic>))
        .toList();
    return items;
  }

  Future<SessionDetail> getSession(String id) async {
    final uri = Uri.parse('${AppConfig.apiBase}/sessions/$id');
    final resp = await _client.get(uri, headers: await _headers());
    if (resp.statusCode == 401) {
      throw HistoryAuthException();
    }
    if (resp.statusCode == 404) {
      throw HistoryNotFoundException(id);
    }
    if (resp.statusCode != 200) {
      throw HistoryApiException('get failed: HTTP ${resp.statusCode}');
    }
    return SessionDetail.fromJson(
        jsonDecode(resp.body) as Map<String, dynamic>);
  }

  /// Deletes a conversation AND its memory footprint server-side
  /// (provenance stripped; sourceless memories removed). Returns the
  /// memory-cascade stats for display: {updated, deleted}.
  Future<Map<String, int>> deleteSession(String id) async {
    final uri = Uri.parse('${AppConfig.apiBase}/sessions/$id');
    final resp = await _client.delete(uri, headers: await _headers());
    if (resp.statusCode == 401) {
      throw HistoryAuthException();
    }
    if (resp.statusCode == 404) {
      throw HistoryNotFoundException(id);
    }
    if (resp.statusCode != 200) {
      throw HistoryApiException('delete failed: HTTP ${resp.statusCode}');
    }
    final body = jsonDecode(resp.body) as Map<String, dynamic>;
    final m = (body['memories'] as Map<String, dynamic>? ?? const {});
    return {
      'updated': (m['memories_updated'] ?? 0) as int,
      'deleted': (m['memories_deleted'] ?? 0) as int,
    };
  }
}

class HistoryApiException implements Exception {
  HistoryApiException(this.message);
  final String message;

  @override
  String toString() => message;
}

class HistoryAuthException extends HistoryApiException {
  HistoryAuthException() : super('Unauthorized — check your API token');
}

class HistoryNotFoundException extends HistoryApiException {
  HistoryNotFoundException(String id) : super('Session not found: $id');
}
