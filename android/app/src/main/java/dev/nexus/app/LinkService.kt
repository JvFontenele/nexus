package dev.nexus.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.os.IBinder
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * Conexão persistente com o PC pareado (A03): recebe share.text/open.url e mantém o link com ping a cada 30 s.
 * Cai a conexão → tenta de novo com espera crescente (2 s … 60 s).
 */
class LinkService : Service(), Pc {
    @Volatile private var running = false
    @Volatile private var session: Session? = null
    private val pending = java.util.concurrent.ConcurrentHashMap<String, java.util.concurrent.CompletableFuture<Pair<String, org.json.JSONObject>>>()
    private val pinger = Executors.newSingleThreadScheduledExecutor()
    @Volatile private var linkText = "Conectando…"
    val audio by lazy {
        AudioReceiver(this) { audioText -> notify(1, status(listOfNotNull(linkText, audioText).joinToString(" · "))) }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_LINK, "Conexão com o PC", NotificationManager.IMPORTANCE_MIN))
        nm.createNotificationChannel(NotificationChannel(CH_PUSH, "Recebido do PC", NotificationManager.IMPORTANCE_HIGH))
        startForeground(1, status("Conectando…"), ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        running = true
        thread(name = "nexus-link") { loop() }
        pinger.scheduleWithFixedDelay({ runCatching { session?.send("ping") } }, 30, 30, TimeUnit.SECONDS)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int) = START_STICKY

    override fun onDestroy() {
        running = false
        audio.stop()
        current = null
        pinger.shutdownNow()
        runCatching { session?.close() }
        super.onDestroy()
    }

    private fun loop() {
        val prefs = getSharedPreferences("nexus", Context.MODE_PRIVATE)
        val kind = if (resources.configuration.smallestScreenWidthDp >= 600) "tablet" else "phone"
        var wait = 2L
        while (running) {
            val host = prefs.getString("host", null)
            val pin = prefs.getString("pin", null)
            if (host == null || pin == null) {
                stopSelf()
                return
            }
            try {
                Session(host, PORT, Identity.load(), pin = pin, presentCert = true).use { s ->
                    s.hello(kind, Build.MODEL, withCert = false)
                    s.timeoutMs = 75_000 // > 2 pings sem resposta = link morto
                    session = s
                    current = this
                    wait = 2
                    linkText = "Conectado a $host"
                    notify(1, status(linkText))
                    while (running) handle(s.recv())
                }
            } catch (e: Exception) {
                session = null
                current = null
                pending.values.forEach { it.completeExceptionally(e) }
                pending.clear()
                if (!running) return
                notify(1, status("Sem conexão (${e.message ?: e.javaClass.simpleName}); nova tentativa em ${wait}s"))
                // shortcut: reconecta por tempo, não por evento de rede; ouvir ConnectivityManager se a espera incomodar
                Thread.sleep(wait * 1000)
                wait = minOf(wait * 2, 60)
            }
        }
    }

    override fun request(type: String, body: org.json.JSONObject, expect: String): org.json.JSONObject {
        val s = session ?: throw ProtocolException("desconectado")
        val id = java.util.UUID.randomUUID().toString()
        val f = java.util.concurrent.CompletableFuture<Pair<String, org.json.JSONObject>>()
        pending[id] = f // antes do send: a resposta pode chegar antes de o send retornar
        try {
            s.send(type, body, id)
            val (t, b) = f.get(30, TimeUnit.SECONDS)
            return responseBody(t, b, expect)
        } catch (e: java.util.concurrent.ExecutionException) {
            throw e.cause ?: e
        } finally {
            pending.remove(id)
        }
    }

    private fun handle(msg: Triple<String, org.json.JSONObject, String?>) {
        val (type, body, re) = msg
        if (re != null) {
            pending.remove(re)?.complete(type to body)
            return // resposta (inclui o pong do ping, que ninguém espera)
        }
        when (type) {
            "share.text" -> {
                val text = body.optString("text")
                // escrever no clipboard em segundo plano é permitido; só a leitura é restrita no Android 10+
                getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("Nexus", text))
                notify(text.hashCode(), push("Texto do PC copiado", text, null))
            }
            "file.offer" -> thread(name = "nexus-file") {
                runCatching { FileTransfer.pull(this, body) }.onFailure {
                    notify(body.hashCode(), push("Arquivo do PC falhou", it.message ?: it.javaClass.simpleName, null))
                }
            }
            "audio.start" -> audio.start()
            "audio.stop" -> audio.stop()
            "open.url" -> {
                val uri = Uri.parse(body.optString("url"))
                if (uri.scheme != "https" && uri.scheme != "http") return
                // Android bloqueia abrir Activity do segundo plano: o usuário toca na notificação para abrir
                val open = PendingIntent.getActivity(this, uri.hashCode(), Intent(Intent.ACTION_VIEW, uri),
                    PendingIntent.FLAG_IMMUTABLE)
                notify(uri.hashCode(), push("Abrir link do PC", uri.toString(), open))
            }
        }
    }

    private fun notify(id: Int, n: Notification) = getSystemService(NotificationManager::class.java).notify(id, n)

    private fun status(text: String): Notification = Notification.Builder(this, CH_LINK)
        .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
        .setContentTitle("Nexus")
        .setContentText(text)
        .setOngoing(true)
        .setContentIntent(PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE))
        .build()

    private fun push(title: String, text: String, tap: PendingIntent?): Notification = Notification.Builder(this, CH_PUSH)
        .setSmallIcon(android.R.drawable.stat_notify_more)
        .setContentTitle(title)
        .setContentText(text)
        .setStyle(Notification.BigTextStyle().bigText(text))
        .setAutoCancel(true)
        .apply { tap?.let { setContentIntent(it) } }
        .build()

    companion object {
        @Volatile private var current: LinkService? = null

        /** A conexão persistente, se estiver de pé. */
        fun connected(): Pc? = current

        /** Liga/desliga o áudio do PC aqui; null se a conexão persistente não estiver ativa. */
        fun toggleAudio(): Boolean? = current?.audio?.let { val on = !it.playing; if (on) it.start() else it.stop(); on }
        private const val CH_LINK = "link"
        private const val CH_PUSH = "push"

        fun start(ctx: Context) = ctx.startForegroundService(Intent(ctx, LinkService::class.java))
        fun stop(ctx: Context) = ctx.stopService(Intent(ctx, LinkService::class.java))
        fun restart(ctx: Context) { stop(ctx); start(ctx) }
    }
}
