package dev.nexus.app

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTimestamp
import android.media.AudioTrack
import android.os.Build
import kotlin.concurrent.thread

/**
 * Toca o áudio do PC (ADR-0004): conexão TLS própria com `audio.attach`, PCM s16 48 kHz estéreo em blocos de 10 ms.
 * Fila de rede acima de 80 ms → descarta blocos para voltar ao tempo real.
 */
class AudioReceiver(private val ctx: Context, private val onStatus: (String?) -> Unit) {
    @Volatile private var session: Session? = null
    @Volatile private var worker: Thread? = null

    val playing get() = worker?.isAlive == true

    fun start() {
        if (playing) return
        worker = thread(name = "nexus-audio") {
            val msg = try {
                run()
                null
            } catch (e: Exception) {
                if (session == null) "Áudio: ${e.message ?: e.javaClass.simpleName}" else null // fechado de propósito: sem erro
            }
            session = null
            onStatus(msg)
        }
    }

    fun stop() {
        val s = session
        session = null
        runCatching { s?.close() }
    }

    private fun run() {
        val prefs = ctx.getSharedPreferences("nexus", Context.MODE_PRIVATE)
        val host = prefs.getString("host", null) ?: throw ProtocolException("sem PC")
        val pin = prefs.getString("pin", null) ?: throw ProtocolException("sem pareamento")
        Session(host, PORT, Identity.load(), pin = pin, presentCert = true).use { s ->
            session = s
            s.send("audio.attach")
            val ready = s.expect("audio.ready")
            val rate = ready.getInt("rate")
            require(ready.getInt("channels") == 2 && ready.getString("format") == "s16le") { "formato não suportado" }
            s.timeoutMs = 5_000
            val bytesPerMs = rate * 4 / 1000
            val track = AudioTrack.Builder()
                .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    // o áudio do PC não volta ao PC quando este aparelho também manda o dele (AudioSender)
                    .setAllowedCapturePolicy(AudioAttributes.ALLOW_CAPTURE_BY_NONE).build())
                .setAudioFormat(AudioFormat.Builder().setSampleRate(rate).setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).build())
                // buffer mínimo: ~metade do atraso; os cortes (underruns) aparecem na notificação
                .setBufferSizeInBytes(AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT))
                .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
            try {
                track.play()
                var written = 0L
                var dropped = 0
                var blocks = 0
                val ts = AudioTimestamp()
                while (session === s) {
                    val buf = s.readBinary()
                    if (s.queued() > 80 * bytesPerMs) { dropped++; continue }
                    track.write(buf, 0, buf.size)
                    written += buf.size / 4
                    if (++blocks % 100 == 0 && track.getTimestamp(ts)) { // ~1 s
                        val trackMs = (written - ts.framePosition) * 1000 / rate
                        val netMs = s.queued() / bytesPerMs
                        val cuts = track.underrunCount
                        onStatus("Áudio do PC · ~${trackMs + netMs + 10} ms" +
                            (if (cuts > 0) " · $cuts cortes" else "") + (if (dropped > 0) " · $dropped descartados" else ""))
                    }
                }
            } finally {
                track.stop()
                track.release()
            }
        }
    }
}
