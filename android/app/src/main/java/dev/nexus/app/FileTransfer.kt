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
                }
            }
            return s.expect("action.result").optString("result")
        }
    }

    /** PC → celular: busca a oferta e salva em Download/Nexus (MediaStore, sem permissão de armazenamento). */
    fun pull(ctx: Context, offer: JSONObject) {
        dataSession(ctx).use { s ->
            s.send("file.pull", JSONObject().put("id", offer.getString("id")))
            val ready = s.expect("file.ready")
            val size = ready.getLong("size")
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
                    }
                }
                resolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
            } catch (e: Exception) {
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
