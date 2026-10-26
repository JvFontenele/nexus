package dev.nexus.app

import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.content.BroadcastReceiver
import android.os.Handler
import android.os.Looper
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.setValue
import org.json.JSONObject

/** Botão Cancelar da notificação de transferência (explícito e não exportado). */
class CancelarTransferencia : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) { FileTransfer.cancelar(intent.getIntExtra("id", 0)) }
}

/** Arquivos entre o celular e o PC: uma conexão TLS de dados por arquivo (file.push / file.pull), sem derrubar a de controle. */
object FileTransfer {
    private const val CHUNK = 256 * 1024

    /** Fechar a conexão de dados é o cancelamento: o PC apaga o .part (ou para de enviar) e emite file.done ok=false. */
    class Cancelado : Exception("cancelado")

    /** Transferências em andamento, para a tela (lista do Compose) e o botão Cancelar da notificação. */
    val ativas = mutableStateListOf<Progresso>()
    /** Quantas terminaram bem: o Nexo dá um pulinho a cada uma. */
    var concluidas by mutableIntStateOf(0)
    private val principal = Handler(Looper.getMainLooper())

    fun cancelar(id: Int) = ativas.toList().find { it.id == id }?.let { it.cancelada = true }

    /** Notificação com barra de progresso (no máximo 4 atualizações por segundo, como no PC) e Cancelar. */
    class Progresso(private val ctx: Context, val titulo: String, val nome: String,
                    private val total: Long, private val icone: Int) {
        private val nm = ctx.getSystemService(NotificationManager::class.java)
        val id = (nome + System.nanoTime()).hashCode()
        private var ultimo = 0L
        var pct by mutableIntStateOf(0)
        @Volatile var cancelada = false
        private val cancelar = android.app.Notification.Action.Builder(null, "Cancelar",
            PendingIntent.getBroadcast(ctx, id, Intent(ctx, CancelarTransferencia::class.java).putExtra("id", id),
                PendingIntent.FLAG_IMMUTABLE)).build()

        init {
            nm.createNotificationChannel(android.app.NotificationChannel("transfer", "Transferências",
                NotificationManager.IMPORTANCE_LOW))
            principal.post { ativas.add(this) }
        }

        fun atualizar(feito: Long) {
            if (cancelada) throw Cancelado()
            val agora = android.os.SystemClock.elapsedRealtime()
            if (agora - ultimo < 250 && feito < total) return
            ultimo = agora
            pct = if (total > 0) (feito * 100 / total).toInt() else 100
            nm.notify(id, android.app.Notification.Builder(ctx, "transfer").setSmallIcon(icone)
                .setContentTitle(titulo).setContentText("$nome · $pct%").setProgress(100, pct, false)
                .setOngoing(true).setOnlyAlertOnce(true).addAction(cancelar).build())
        }

        fun fim(ok: Boolean = false) {
            nm.cancel(id)
            principal.post { ativas.remove(this); if (ok) concluidas++ }
        }
    }

    private fun dataSession(ctx: Context): Session {
        val prefs = ctx.getSharedPreferences("nexus", Context.MODE_PRIVATE)
        val host = prefs.getString("host", null) ?: throw ProtocolException("pareie com o PC primeiro")
        val pin = prefs.getString("pin", null) ?: throw ProtocolException("pareie com o PC primeiro")
        return Session(host, PORT, Identity.load(), pin = pin, presentCert = true).also { it.timeoutMs = 60_000 }
    }

    /** Celular → PC. Devolve o nome com que o PC salvou. */
    fun push(ctx: Context, uri: Uri): String {
        var name = "arquivo"
        var size = -1L
        ctx.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                name = c.getString(0) ?: name
                if (!c.isNull(1)) size = c.getLong(1)
            }
        }
        if (size < 0) throw ProtocolException("tamanho desconhecido")
        val progresso = Progresso(ctx, "Enviando ao PC", name, size, android.R.drawable.stat_sys_upload)
        try {
            dataSession(ctx).use { s ->
                s.send("file.push", JSONObject().put("name", name).put("size", size))
                s.expect("file.ready")
                val buf = ByteArray(CHUNK)
                var sent = 0L
                ctx.contentResolver.openInputStream(uri)!!.use { input ->
                    while (sent < size) {
                        val n = input.read(buf, 0, minOf(CHUNK.toLong(), size - sent).toInt())
                        if (n < 0) throw ProtocolException("arquivo encurtou durante o envio")
                        s.sendBinary(buf, n)
                        sent += n
                        progresso.atualizar(sent)
                    }
                }
                return s.expect("action.result").optString("result").also { progresso.fim(ok = true) }
            }
        } finally {
            progresso.fim()
        }
    }

    /** PC → celular: busca a oferta e salva em Download/Nexus (MediaStore, sem permissão de armazenamento). */
    fun pull(ctx: Context, offer: JSONObject) {
        dataSession(ctx).use { s ->
            s.send("file.pull", JSONObject().put("id", offer.getString("id")))
            val ready = s.expect("file.ready")
            val size = ready.getLong("size")
            val progresso = Progresso(ctx, "Recebendo do PC", ready.getString("name"), size,
                android.R.drawable.stat_sys_download)
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, ready.getString("name"))
                put(MediaStore.Downloads.RELATIVE_PATH, "Download/Nexus")
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val resolver = ctx.contentResolver
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values) ?: throw ProtocolException("sem espaço")
            try {
                resolver.openOutputStream(uri)!!.use { out ->
                    var got = 0L
                    while (got < size) {
                        val chunk = s.readBinary()
                        out.write(chunk)
                        got += chunk.size
                        progresso.atualizar(got)
                    }
                }
                progresso.fim(ok = true)
                resolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
            } catch (e: Exception) {
                progresso.fim()
                resolver.delete(uri, null, null) // nada de arquivo pela metade
                throw e
            }
            val open = PendingIntent.getActivity(ctx, uri.hashCode(),
                Intent(Intent.ACTION_VIEW, uri).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION), PendingIntent.FLAG_IMMUTABLE)
            ctx.getSystemService(NotificationManager::class.java).notify(uri.hashCode(),
                android.app.Notification.Builder(ctx, "push").setSmallIcon(android.R.drawable.stat_sys_download_done)
                    .setContentTitle("Arquivo do PC").setContentText(ready.getString("name"))
                    .setContentIntent(open).setAutoCancel(true).build())
        }
    }
}
