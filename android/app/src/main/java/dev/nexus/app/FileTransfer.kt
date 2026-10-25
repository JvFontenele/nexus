package dev.nexus.app

import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.MediaStore
import android.provider.OpenableColumns
import org.json.JSONObject

/** Arquivos entre o celular e o PC: uma conexão TLS de dados por arquivo (file.push / file.pull), sem derrubar a de controle. */
object FileTransfer {
    private const val CHUNK = 256 * 1024

    /** Notificação com barra de progresso (no máximo 4 atualizações por segundo, como no PC). */
    private class Progresso(private val ctx: Context, private val titulo: String, private val nome: String,
                            private val total: Long, private val icone: Int) {
        private val nm = ctx.getSystemService(NotificationManager::class.java)
        private val id = (nome + System.nanoTime()).hashCode()
        private var ultimo = 0L

        init {
            nm.createNotificationChannel(android.app.NotificationChannel("transfer", "Transferências",
                NotificationManager.IMPORTANCE_LOW))
        }

        fun atualizar(feito: Long) {
            val agora = android.os.SystemClock.elapsedRealtime()
            if (agora - ultimo < 250 && feito < total) return
            ultimo = agora
            val pct = if (total > 0) (feito * 100 / total).toInt() else 100
            nm.notify(id, android.app.Notification.Builder(ctx, "transfer").setSmallIcon(icone)
                .setContentTitle(titulo).setContentText("$nome · $pct%").setProgress(100, pct, false)
                .setOngoing(true).setOnlyAlertOnce(true).build())
        }

        fun fim() = nm.cancel(id)
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
                return s.expect("action.result").optString("result")
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
                progresso.fim()
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
