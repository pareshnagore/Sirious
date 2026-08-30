package com.sirious.sirious

import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Handler
import android.os.Looper
import io.flutter.plugin.common.EventChannel
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

/**
 * Audio-route plumbing for Phase 6 speaker mode.
 *
 * - `set_speaker_comm_device` (MethodChannel): routes playback/capture through
 *   the MODERN communication-device API (AudioManager.setCommunicationDevice)
 *   — the same path ChatGPT/Perplexity drive. Combined with a
 *   VOICE_COMMUNICATION capture source this gives full-duplex platform AEC
 *   on SM-E346B/Android 16 (probe-verified 30 Aug).
 * - `sirious/audio_route` EventChannel: emits "route_changed" on output-device
 *   changes (speaker <-> BT <-> earphone), debounced 500 ms natively. The Dart
 *   side rebuilds the (dormant) software-AEC pipeline on route changes.
 */
class MainActivity : FlutterActivity() {

    private var routeEvents: EventChannel.EventSink? = null
    private val mainHandler = Handler(Looper.getMainLooper())
    private var debouncePending = false

    private val deviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>) {
            scheduleRouteEvent()
        }

        override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>) {
            scheduleRouteEvent()
        }
    }

    private fun scheduleRouteEvent() {
        if (debouncePending) return
        debouncePending = true
        mainHandler.postDelayed({
            debouncePending = false
            routeEvents?.success("route_changed")
        }, ROUTE_DEBOUNCE_MS)
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            METHOD_CHANNEL,
        ).setMethodCallHandler { call, result ->
            when (call.method) {
                "set_speaker_comm_device" -> {
                    try {
                        val am = getSystemService(AudioManager::class.java)
                        val speaker = am.availableCommunicationDevices
                            .firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
                        val ok = speaker != null && am.setCommunicationDevice(speaker)
                        result.success(if (ok) "ok" else "failed")
                    } catch (e: Exception) {
                        result.error("comm_device", e.message, null)
                    }
                }
                "get_audio_route" -> {
                    // Route-aware capture profiles (Phase 6 Stage C):
                    // classify the CURRENT output route. Any wired/BT/USB
                    // headset output counts as earphones; otherwise the
                    // builtin speaker is assumed.
                    try {
                        val am = getSystemService(AudioManager::class.java)
                        val hasHeadset = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
                            .any {
                                it.type == AudioDeviceInfo.TYPE_WIRED_HEADPHONES ||
                                    it.type == AudioDeviceInfo.TYPE_WIRED_HEADSET ||
                                    it.type == AudioDeviceInfo.TYPE_USB_HEADSET ||
                                    it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP ||
                                    it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO
                            }
                        result.success(if (hasHeadset) "earphones" else "speaker")
                    } catch (e: Exception) {
                        result.error("audio_route", e.message, null)
                    }
                }
                "clear_comm_device" -> {
                    // Undo a previous speaker-mode setCommunicationDevice
                    // pin so playback follows the natural route again.
                    try {
                        getSystemService(AudioManager::class.java)
                            .clearCommunicationDevice()
                        result.success("ok")
                    } catch (e: Exception) {
                        result.error("comm_device", e.message, null)
                    }
                }
                else -> result.notImplemented()
            }
        }

        EventChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            ROUTE_CHANNEL,
        ).setStreamHandler(object : EventChannel.StreamHandler {
            override fun onListen(args: Any?, events: EventChannel.EventSink?) {
                routeEvents = events
                getSystemService(AudioManager::class.java)
                    .registerAudioDeviceCallback(deviceCallback, mainHandler)
            }

            override fun onCancel(args: Any?) {
                getSystemService(AudioManager::class.java)
                    .unregisterAudioDeviceCallback(deviceCallback)
                routeEvents = null
            }
        })
    }


    override fun onDestroy() {
        try {
            getSystemService(AudioManager::class.java)
                .unregisterAudioDeviceCallback(deviceCallback)
        } catch (_: Exception) {
        }
        super.onDestroy()
    }

    companion object {
        private const val METHOD_CHANNEL = "sirious/audio_route"
        private const val ROUTE_CHANNEL = "sirious/audio_route_events"
        private const val ROUTE_DEBOUNCE_MS = 500L
    }
}
