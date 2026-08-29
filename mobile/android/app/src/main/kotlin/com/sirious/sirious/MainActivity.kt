package com.sirious.sirious

import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Handler
import android.os.Looper
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel

/**
 * Phase 6 Stage B (B4): reports audio-route (output device) changes to Dart.
 *
 * A route change (speaker <-> BT <-> earphone) shifts the acoustic echo path,
 * so the AEC3 delay model must be rebuilt (fresh AecPipeline) - see
 * sirious_session_controller._onAudioRouteChanged().
 *
 * Debounced on the native side: some devices fire several callbacks for one
 * physical change; 500 ms coalesces them.
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
            // Already unregistered (onCancel) or engine torn down.
        }
        super.onDestroy()
    }

    companion object {
        private const val ROUTE_CHANNEL = "sirious/audio_route"
        private const val ROUTE_DEBOUNCE_MS = 500L
    }
}
