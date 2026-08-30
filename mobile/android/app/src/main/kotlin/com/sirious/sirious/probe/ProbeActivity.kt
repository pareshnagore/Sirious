package com.sirious.sirious.probe

import android.app.Activity
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.ScrollView
import android.widget.TextView
import java.io.File
import kotlin.math.abs
import kotlin.math.min
import kotlin.math.sin
import kotlin.random.Random

/**
 * Phase 6 Stage B probe host. Auto-runs [PlatformAecProbe] on launch under
 * TWO playback conditions and shows/persists the report:
 *
 *   B1: MODE_NORMAL          + USAGE_MEDIA playback            (Sirious today)
 *   B2: MODE_IN_COMMUNICATION + setCommunicationDevice(SPEAKER)
 *       + USAGE_VOICE_COMMUNICATION playback                   (vendor path)
 *
 * Report is appended to getExternalFilesDir(null)/platform_aec_probe.log
 * (adb-pullable on release builds) and rendered on screen.
 */
class ProbeActivity : Activity() {

    private lateinit var audioManager: AudioManager
    private lateinit var output: TextView
    private val main = Handler(Looper.getMainLooper())

    private val logBuilder = StringBuilder()

    private fun log(s: String) {
        logBuilder.appendLine(s)
        main.post {
            output.append("$s\n")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        audioManager = getSystemService(AudioManager::class.java)

        val scroll = ScrollView(this)
        output = TextView(this).apply {
            setPadding(32, 48, 32, 32)
            textSize = 13f
            setTextIsSelectable(true)
        }
        scroll.addView(output)
        setContentView(scroll)

        Thread {
            try {
                runProbe()
            } catch (e: Exception) {
                log("PROBE EXCEPTION ${e.javaClass.simpleName}: ${e.message}")
                for (el in e.stackTrace.take(6)) log("    at $el")
            }
            persistReport()
        }.start()
    }

    // ── loud deterministic test signal: shaped noise, speech-band, slow envelope ─
    private fun buildTestSignal(seconds: Int, sampleRate: Int): ShortArray {
        val n = seconds * sampleRate
        val out = ShortArray(n)
        val rng = Random(42)
        var lp = 0.0
        for (i in 0 until n) {
            val env = 0.6 + 0.4 * sin(2 * Math.PI * 2.5 * i / sampleRate) // 2.5 Hz AM
            lp = 0.35 * lp + 0.65 * (rng.nextDouble() * 2 - 1) // crude lowpass
            val speechish = 0.5 * lp + 0.5 * sin(2 * Math.PI * 320 * i / sampleRate)
            out[i] = (min(0.92, abs(speechish * env) * 0.92) * Short.MAX_VALUE *
                (if (speechish < 0) -1 else 1)).toInt().toShort()
        }
        return out
    }

    private fun makeTrack(usage: Int): Pair<AudioTrack, ShortArray> {
        val pcm = buildTestSignal( seconds = 12, sampleRate = 48000)
        val attrs = AudioAttributes.Builder()
            .setUsage(usage)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build()
        val track = AudioTrack.Builder()
            .setAudioAttributes(attrs)
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(48000)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build(),
            )
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setBufferSizeInBytes(48000 * 2) // 0.5 s @48k mono
            .build()
        return track to pcm
    }

    private fun play(track: AudioTrack, pcm: ShortArray): Boolean {
        return try {
            track.play()
            Thread {
                var off = 0
                while (off < pcm.size && track.playState == AudioTrack.PLAYSTATE_PLAYING) {
                    off += track.write(pcm, off, min(4800, pcm.size - off))
                }
            }.start()
            true
        } catch (e: Exception) {
            log("PROBE play failed: $e")
            false
        }
    }

    private fun runProbe() {
        val mediaVol = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC)
        val callVol = audioManager.getStreamVolume(AudioManager.STREAM_VOICE_CALL)
        audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, mediaVol, 0)
        audioManager.setStreamVolume(
            AudioManager.STREAM_VOICE_CALL,
            audioManager.getStreamMaxVolume(AudioManager.STREAM_VOICE_CALL), 0,
        )

        // ── condition B1: MODE_NORMAL + USAGE_MEDIA (Sirious today) ─────────────
        log("PROBE ===== CONDITION B1: MODE_NORMAL + USAGE_MEDIA =====")
        audioManager.mode = AudioManager.MODE_NORMAL
        var report1 = ""
        val (track1, pcm1) = makeTrack(AudioAttributes.USAGE_MEDIA)
        report1 = PlatformAecProbe.run(
            audioManager,
            startPlayback = { play(track1, pcm1) },
            stopPlayback = {
                try { track1.pause(); track1.flush() } catch (_: Exception) {}
            },
            chunksPerPhase = 100,
        )
        log(report1)
        try { track1.release() } catch (_: Exception) {}

        // ── condition B2: MODE_IN_COMMUNICATION + setCommunicationDevice ────────
        log("PROBE ===== CONDITION B2: IN_COMMUNICATION + setCommunicationDevice =====")
        audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
        var report2 = ""
        val (track2, pcm2) = makeTrack(AudioAttributes.USAGE_VOICE_COMMUNICATION)
        report2 = PlatformAecProbe.run(
            audioManager,
            startPlayback = { play(track2, pcm2) },
            stopPlayback = {
                try { track2.pause(); track2.flush() } catch (_: Exception) {}
            },
            chunksPerPhase = 100,
        )
        log(report2)
        try { track2.release() } catch (_: Exception) {}

        audioManager.mode = AudioManager.MODE_NORMAL
        audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, mediaVol, 0)
        audioManager.setStreamVolume(AudioManager.STREAM_VOICE_CALL, callVol, 0)
        log("PROBE DONE volumes restored")
    }

    private fun persistReport() {
        try {
            val dir = getExternalFilesDir(null) ?: return
            File(dir, "platform_aec_probe.log")
                .appendText(logBuilder.toString() + "\n\n")
            log("PROBE report persisted")
        } catch (e: Exception) {
            android.util.Log.e(PlatformAecProbe.TAG, "persist failed", e)
        }
    }
}
