package com.sirious.sirious.probe

import android.annotation.SuppressLint
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.util.Log
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * Phase 6 Stage B probe: is the PLATFORM AEC usable on this device via the
 * MODERN communication-device path (setCommunicationDevice + VOICE_COMMUNICATION)?
 *
 * Protocol (mirrors the 25 Aug test, which used the LEGACY speakerphone path
 * and concluded "platform AEC dead on Samsung" — never retested since):
 *
 *   Phase A  baseline: VOICE_COMMUNICATION capture, no communication device
 *            set, TTS NOT playing          -> expected: low RMS (room tone)
 *   Phase B  playback:  same capture while a loud tone/speech file plays
 *            on the SPEAKER                -> KEY QUESTION: is uplink silent
 *                                              (call-pipeline mute, the 25 Aug
 *                                              result) or carrying echo
 *                                              (uplink alive)?
 *   Phase C  after playback stops        -> back to room tone
 *
 * The probe records what audio_flinger actually gives the app in each phase:
 * per-phase chunk RMS stats (min/p50/peak), plus whether
 * AcousticEchoCanceler.isAvailable() and which communication device was set.
 *
 * No Sirious audio pipeline is touched; standalone Activity, plain AudioRecord.
 */
@SuppressLint("MissingPermission")
object PlatformAecProbe {

    const val TAG = "PlatformAecProbe"

    data class PhaseStats(
        val name: String,
        var chunks: Int = 0,
        var minRms: Double = Double.MAX_VALUE,
        var maxRms: Double = 0.0,
        val rmsValues: MutableList<Double> = mutableListOf(),
    ) {
        fun add(rms: Double) {
            chunks++
            if (rms < minRms) minRms = rms
            if (rms > maxRms) maxRms = rms
            rmsValues.add(rms)
        }

        fun p50(): Double {
            if (rmsValues.isEmpty()) return 0.0
            val s = rmsValues.sorted()
            return s[s.size / 2]
        }

        override fun toString(): String =
            "$name chunks=$chunks min=${"%.0f".format(minRms)} " +
                "p50=${"%.0f".format(p50())} peak=${"%.0f".format(maxRms)}"
    }

    /**
     * Runs the probe. [startPlayback] / [stopPlayback] are supplied by the
     * caller (Activity) so the probe stays audio-stack-agnostic: they must
     * play/stop a LOUD signal on the speaker with ~100 ms latency.
     * Returns a human-readable report.
     */
    fun run(
        audioManager: AudioManager,
        startPlayback: () -> Boolean,
        stopPlayback: () -> Unit,
        chunksPerPhase: Int = 150, // ~15 s per phase at 100 ms chunks
    ): String {
        val report = StringBuilder()
        fun line(s: String) {
            report.appendLine(s)
            Log.i(TAG, s)
        }

        // ---- environment facts -------------------------------------------------
        line("PROBE device=${Build.MODEL} sdk=${Build.VERSION.SDK_INT}")
        val aecAvailable = android.media.audiofx.AcousticEchoCanceler.isAvailable()
        line("PROBE AcousticEchoCanceler.isAvailable()=$aecAvailable")

        // ---- pick the speaker as communication device (modern path) ------------
        val speaker: AudioDeviceInfo? = audioManager.availableCommunicationDevices
            .firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
        if (speaker == null) {
            line("PROBE FAIL no builtin speaker communication device found")
            return report.toString()
        }
        val setOk = audioManager.setCommunicationDevice(speaker)
        line("PROBE setCommunicationDevice(SPEAKER)=$setOk " +
            "active=${audioManager.communicationDevice?.typeName}")

        // ---- recorder: VOICE_COMMUNICATION, 16 kHz mono, 100 ms chunks ---------
        val minBuf = AudioRecord.getMinBufferSize(
            16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
        )
        val record = AudioRecord(
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            16000,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            minBuf * 4,
        )
        val aec = if (aecAvailable) {
            android.media.audiofx.AcousticEchoCanceler.create(record.audioSessionId)
        } else null
        line("PROBE AEC effect create=${aec != null}")

        val chunkShorts = 1600 // 100 ms @ 16 kHz
        val buf = ShortArray(chunkShorts)

        fun rmsOf(s: ShortArray): Double {
            var sum = 0.0
            for (v in s) sum += v.toDouble() * v
            return sqrt(sum / s.size)
        }

        fun capturePhase(name: String, stats: PhaseStats) {
            record.startRecording()
            var captured = 0
            while (captured < chunksPerPhase) {
                val n = record.read(buf, 0, chunkShorts)
                if (n > 0) {
                    stats.add(rmsOf(buf.copyOf(n)))
                    captured++
                }
            }
            record.stop()
            line("PROBE $stats")
        }

        // ---- Phase A: silence baseline ----------------------------------------
        val a = PhaseStats("A_baseline_no_playback")
        capturePhase("A", a)

        // ---- Phase B: playback on speaker --------------------------------------
        val b = PhaseStats("B_playback_speaker")
        val started = startPlayback()
        line("PROBE startPlayback=$started")
        if (started) {
            Thread.sleep(300) // let playout stabilize
            capturePhase("B", b)
            stopPlayback()
        }

        // ---- Phase C: recovery -------------------------------------------------
        Thread.sleep(300)
        val c = PhaseStats("C_after_playback")
        capturePhase("C", c)

        // ---- verdict ------------------------------------------------------------
        line("PROBE ---- VERDICT ----")
        line("PROBE B.peak=${"%.0f".format(b.maxRms)} B.p50=${"%.0f".format(b.p50())}")
        when {
            !started -> line("PROBE VERDICT: INVALID (playback failed to start)")
            b.p50() < 50.0 && b.maxRms < 300.0 -> line(
                "PROBE VERDICT: MUTE — uplink is silent during playback " +
                    "(call-pipeline gate; legacy 25 Aug result reproduced)",
            )
            b.maxRms > 300.0 -> line(
                "PROBE VERDICT: ALIVE — uplink carries audio during playback " +
                    "(peak ${"%.0f".format(b.maxRms)}). Platform AEC residual vs raw " +
                    "echo needs the A/B loudness comparison; see C vs A floor.",
            )
            else -> line("PROBE VERDICT: AMBIGUOUS — inspect per-phase stats")
        }

        record.release()
        aec?.release()
        audioManager.clearCommunicationDevice()
        return report.toString()
    }

    private val AudioDeviceInfo.typeName: String
        get() = when (type) {
            AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "SPEAKER"
            AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "EARPIECE"
            AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "HEADPHONES"
            AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "BT_A2DP"
            AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "BT_SCO"
            else -> "type=$type"
        }
}
