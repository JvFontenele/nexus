package dev.nexus.app

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.projection.MediaProjection
import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import kotlin.concurrent.thread

/**
 * Manda o áudio dos apps deste aparelho para tocar no PC: captura de reprodução do Android 10+ (MediaProjection,
 * confirmada pelo usuário a cada sessão), PCM s16 48 kHz estéreo em blocos de 10 ms por uma conexão `audio.push`.
 * Apps que proíbem a captura (e as ligações) ficam de fora: é regra do Android.
 */
class AudioSender(private val ctx: Context, private val onEnd: () -> Unit, private val onStatus: (String?) -> Unit) {
    @Volatile private var session: Session? = null
    @Volatile private var worker: Thread? = null
    @Volatile private var projection: MediaProjection? = null

    val sending get() = worker?.isAlive == true

    /** `mp` vem da confirmação do usuário; este objeto passa a ser o dono dela e a encerra ao parar. */
    fun start(mp: MediaProjection) {
        if (sending) { mp.stop(); return }
        projection = mp
        // o usuário pode parar pelo próprio Android (chip de gravação): para junto
        mp.registerCallback(object : MediaProjection.Callback() { override fun onStop() { stop() } }, Handler(Looper.getMainLooper()))
        worker = thread(name = "nexus-audio-out") {
            val msg = try {
                run(mp)
                null
            } catch (e: Exception) {
                if (session == null) null else "Não foi: ${(e.message ?: e.javaClass.simpleName).removePrefix("forbidden: ")}"
            }
            session = null
            projection?.let { projection = null; runCatching { it.stop() } }
            onStatus(msg)
            onEnd()
        }
    }

    fun stop() {
        val s = session
        session = null
        runCatching { s?.close() }
        projection?.let { projection = null; runCatching { it.stop() } }
    }

    @SuppressLint("MissingPermission") // RECORD_AUDIO é pedido pela tela antes da confirmação
    private fun run(mp: MediaProjection) {
        val prefs = ctx.getSharedPreferences("nexus", Context.MODE_PRIVATE)
        val host = prefs.getString("host", null) ?: throw ProtocolException("sem PC")
        val pin = prefs.getString("pin", null) ?: throw ProtocolException("sem pareamento")
        val capture = AudioPlaybackCaptureConfiguration.Builder(mp)
            .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
            .addMatchingUsage(AudioAttributes.USAGE_GAME)
            .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
            .build()
        val rec = AudioRecord.Builder()
            .setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(RATE)
                .setChannelMask(AudioFormat.CHANNEL_IN_STEREO).build())
            .setBufferSizeInBytes(maxOf(AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_STEREO,
                AudioFormat.ENCODING_PCM_16BIT), BLOCK * 8))
            .setAudioPlaybackCaptureConfig(capture)
            .build()
        try {
            Session(host, PORT, Identity.load(), pin = pin, presentCert = true).use { s ->
                session = s
                s.timeoutMs = 75_000 // sem a permissão, o PC pergunta ao usuário (até 60 s)
                s.request("audio.push", JSONObject().put("rate", RATE).put("channels", 2).put("format", "s16le"), "audio.ready")
                rec.startRecording()
                onStatus("Mandando o áudio para o PC")
                val buf = ByteArray(BLOCK)
                while (session === s) {
                    var off = 0
                    while (off < BLOCK) {
                        val n = rec.read(buf, off, BLOCK - off)
                        if (n < 0) throw ProtocolException("captura falhou ($n)")
                        off += n
                    }
                    s.sendBinary(buf, BLOCK) // rede lenta: bloqueia aqui e o Android descarta o que não coube
                }
            }
        } finally {
            runCatching { rec.stop() }
            rec.release()
        }
    }

    companion object {
        const val RATE = 48_000
        const val BLOCK = RATE / 100 * 4 // 10 ms de s16 estéreo = 1920 bytes
    }
}
