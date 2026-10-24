package dev.nexus.app

import android.app.Activity
import android.content.ClipboardManager
import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import kotlin.concurrent.thread

/**
 * Envia texto ao PC: do menu Compartilhar (ACTION_SEND) ou do bloco rápido "Clipboard → PC" (SEND_CLIPBOARD).
 * O Android 10+ só deixa ler o clipboard com a janela em foco: por isso o bloco abre esta Activity transparente.
 */
class ShareActivity : Activity() {
    private var sent = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        @Suppress("DEPRECATION") // a forma tipada só existe a partir do Android 13; o app vai até o 10
        val uris = when (intent?.action) {
            Intent.ACTION_SEND -> listOfNotNull(intent.getParcelableExtra<android.net.Uri>(Intent.EXTRA_STREAM))
            Intent.ACTION_SEND_MULTIPLE -> intent.getParcelableArrayListExtra<android.net.Uri>(Intent.EXTRA_STREAM).orEmpty()
            else -> emptyList()
        }
        if (uris.isNotEmpty()) sendFiles(uris)
        else if (intent?.action == Intent.ACTION_SEND) send(intent.getStringExtra(Intent.EXTRA_TEXT))
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus && intent?.action == ACTION_CLIPBOARD) {
            val clip = getSystemService(ClipboardManager::class.java).primaryClip
            send(clip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this)?.toString())
        }
    }

    private fun send(text: String?) {
        if (sent) return
        sent = true
        if (text.isNullOrEmpty()) {
            done("Nexus: nada para enviar")
            return
        }
        thread {
            val msg = try {
                when (withPc(this) { it.shareText(text.take(65536)) }) {
                    "copiado" -> "Nexus: copiado no PC"
                    else -> "Nexus: enviado ao PC (sem clipboard, só notificação)"
                }
            } catch (e: Exception) {
                "Nexus: falhou (${e.message ?: e.javaClass.simpleName})"
            }
            runOnUiThread { done(msg) }
        }
    }

    private fun sendFiles(uris: List<android.net.Uri>) {
        sent = true
        Toast.makeText(applicationContext, "Nexus: enviando ${uris.size} arquivo(s) ao PC…", Toast.LENGTH_SHORT).show()
        val app = applicationContext
        thread {
            val ok = uris.count { runCatching { FileTransfer.push(app, it) }.isSuccess }
            android.os.Handler(android.os.Looper.getMainLooper()).post {
                Toast.makeText(app, "Nexus: $ok de ${uris.size} arquivo(s) no PC (Downloads/Nexus)", Toast.LENGTH_LONG).show()
            }
        }
        finish() // o envio continua em segundo plano; a tela transparente não fica presa
    }

    private fun done(msg: String) {
        Toast.makeText(applicationContext, msg, Toast.LENGTH_LONG).show()
        finish()
    }

    companion object {
        const val ACTION_CLIPBOARD = "dev.nexus.app.SEND_CLIPBOARD"
    }
}
