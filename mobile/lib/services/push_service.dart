/// FCM setup for reminders (Phase 4 chunk 4).
///
/// Token fetch + backend registration + background handler. All best-effort
/// with debugPrint logging: a push problem must never block app startup or
/// the voice path.
library;

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../config/app_config.dart';

class PushService {
  PushService._();

  static bool _initialized = false;

  /// Call once from main() before runApp().
  static Future<void> init() async {
    if (_initialized) return;
    try {
      await Firebase.initializeApp();
      _initialized = true;

      // Background/terminated messages need a top-level handler.
      FirebaseMessaging.onBackgroundMessage(
          _firebaseMessagingBackgroundHandler);

      // Ask once (Android 13+ POST_NOTIFICATIONS). If denied we still
      // register — FCM tokens exist regardless; only display is affected.
      await FirebaseMessaging.instance.requestPermission(
        alert: true,
        badge: true,
        sound: true,
      );

      final token = await FirebaseMessaging.instance.getToken();
      if (token != null) {
        await registerToken(token);
      }

      // Token rotation while the app is running.
      FirebaseMessaging.instance.onTokenRefresh.listen((newToken) async {
        debugPrint('[push] token refreshed');
        await registerToken(newToken);
      });

      // Foreground pushes: FCM shows nothing by default in foreground, so
      // surface them via debugPrint for now (UI toast is later polish).
      FirebaseMessaging.onMessage.listen((RemoteMessage msg) {
        debugPrint(
          '[push] foreground message: ${msg.notification?.title} '
          '/ ${msg.notification?.body}',
        );
      });
    } catch (e) {
      debugPrint('[push] init failed (non-fatal): $e');
    }
  }

  /// POST the FCM token to the backend (bearer-auth like all REST).
  static Future<bool> registerToken(String token) async {
    try {
      const storage = FlutterSecureStorage();
      final authToken = await storage.read(key: 'auth_token') ?? '';
      final resp = await http.post(
        Uri.parse('${AppConfig.apiBase}/devices/register'),
        headers: {
          'Authorization': 'Bearer $authToken',
          'Content-Type': 'application/json',
        },
        body: '{"token": "$token"}',
      );
      debugPrint('[push] registered (${resp.statusCode})');
      return resp.statusCode == 200;
    } catch (e) {
      // Backend briefly unreachable → retry on next app start.
      debugPrint('[push] registration failed (will retry on next start): $e');
      return false;
    }
  }
}

/// Top-level handler required by FCM for background/terminated delivery.
/// MUST NOT touch UI or plugin channels beyond firebase APIs. Notification
/// display itself is handled by FCM's default when the message carries a
/// `notification` payload — no work needed here.
@pragma('vm:entry-point')
Future<void> _firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  await Firebase.initializeApp(); // required in background isolate
  debugPrint('[push] background message: ${message.notification?.title}');
}
