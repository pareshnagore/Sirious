import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Stores the Sirious API bearer token in the device secure storage
/// (Android Keystore-backed). The token gates both the REST history API
/// and the /ws handshake on the server (Phase 2).
class AuthService {
  AuthService();

  static const _storageKey = 'sirious_api_token';
  final FlutterSecureStorage _storage = const FlutterSecureStorage();

  Future<String?> getToken() => _storage.read(key: _storageKey);

  Future<void> saveToken(String token) =>
      _storage.write(key: _storageKey, value: token.trim());

  Future<bool> hasToken() async => await getToken() != null;
}
