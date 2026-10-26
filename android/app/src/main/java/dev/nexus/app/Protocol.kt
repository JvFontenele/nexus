package dev.nexus.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.Closeable
import java.io.DataInputStream
import java.io.DataOutputStream
import java.net.InetSocketAddress
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.UUID
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager

// Protocolo Nexus v1 (docs/protocol/v1.md), lado cliente.

const val PORT = 47210
private const val MAX_FRAME = 1024 * 1024
private val HEX64 = Regex("[0-9a-f]{64}")

fun sha256Hex(vararg parts: ByteArray): String {
    val md = MessageDigest.getInstance("SHA-256")
    parts.forEach { md.update(it) }
    return md.digest().joinToString("") { "%02x".format(it) }
}

private fun hexBytes(h: String): ByteArray {
    require(HEX64.matches(h)) { "hex de 64 esperado" }
    return ByteArray(32) { h.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
}

fun newNonce(): String = ByteArray(32).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }

fun commit(nC: String, fpC: String, fpS: String) = sha256Hex(hexBytes(nC), hexBytes(fpC), hexBytes(fpS))

fun sasCode(session: String, fpS: String, fpC: String, nS: String, nC: String): String {
    val h = sha256Hex("nexus-pair-v1".toByteArray(), session.toByteArray(), hexBytes(fpS), hexBytes(fpC), hexBytes(nS), hexBytes(nC))
    return "%06d".format(h.substring(0, 8).toLong(16) % 1_000_000)
}

class ProtocolException(msg: String) : Exception(msg)

/** Sem pin: aceita o certificado só para o pareamento (o SAS prova a identidade). Com pin: exige o fingerprint exato. */
private class PinTrust(private val pin: String?) : X509TrustManager {
    var seen: String? = null
    override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String?) {
        val fp = sha256Hex(chain[0].encoded)
        if (pin != null && !MessageDigest.isEqual(fp.toByteArray(), pin.toByteArray()))
            throw CertificateException("a identidade do PC mudou; pareie de novo")
        seen = fp
    }
    override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String?) = throw CertificateException()
    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
}

/** Pedido/resposta ao PC: pela conexão persistente (LinkService) ou por uma sessão avulsa. */
interface Pc {
    fun request(type: String, body: JSONObject, expect: String): JSONObject
}

/** Corpo da resposta se o tipo bate; `error` do PC vira exceção. */
fun responseBody(t: String, body: JSONObject, expect: String): JSONObject = when (t) {
    expect -> body
    "error" -> throw ProtocolException("${body.optString("code")}: ${body.optString("message")}")
    else -> throw ProtocolException("esperava $expect, veio $t")
}

class Session(host: String, port: Int, private val id: Identity, pin: String?, presentCert: Boolean) : Closeable, Pc {
    private val sock: SSLSocket
    private val input: DataInputStream
    private val output: DataOutputStream
    val serverFp: String

    init {
        val trust = PinTrust(pin)
        val ctx = SSLContext.getInstance("TLSv1.3")
        ctx.init(if (presentCert) arrayOf(id.keyManager) else null, arrayOf(trust), SecureRandom())
        sock = ctx.socketFactory.createSocket() as SSLSocket
        try {
            sock.connect(InetSocketAddress(host, port), 10_000)
            sock.enabledProtocols = arrayOf("TLSv1.3")
            sock.soTimeout = 15_000
            sock.startHandshake()
        } catch (e: Exception) {
            sock.close()
            throw e
        }
        serverFp = trust.seen ?: throw ProtocolException("sem certificado do PC")
        input = DataInputStream(sock.inputStream.buffered())
        output = DataOutputStream(sock.outputStream.buffered())
    }

    var timeoutMs: Int
        get() = sock.soTimeout
        set(v) { sock.soTimeout = v }

    @Synchronized // a conexão persistente envia ping de outra thread
    fun send(type: String, body: JSONObject = JSONObject(), msgId: String = UUID.randomUUID().toString()) {
        val msg = JSONObject().put("v", 1).put("id", msgId).put("type", type).put("body", body)
        val bytes = msg.toString().toByteArray()
        output.writeInt(bytes.size)
        output.write(bytes)
        output.flush()
    }

    /** Próxima mensagem como (type, body, re). */
    fun recv(): Triple<String, JSONObject, String?> {
        val n = input.readInt()
        if (n <= 0 || n > MAX_FRAME) throw ProtocolException("frame inválido")
        val buf = ByteArray(n).also { input.readFully(it) }
        val msg = JSONObject(String(buf))
        if (msg.optInt("v") != 1) throw ProtocolException("versão inválida")
        val body = msg.optJSONObject("body") ?: throw ProtocolException("body inválido")
        return Triple(msg.optString("type"), body, msg.optString("re").ifEmpty { null })
    }

    @Synchronized
    fun sendBinary(data: ByteArray, len: Int) {
        output.writeInt(len)
        output.write(data, 0, len)
        output.flush()
    }

    /** Frame binário (áudio): 4 bytes de tamanho + dados. */
    fun readBinary(): ByteArray {
        val n = input.readInt()
        if (n <= 0 || n > MAX_FRAME) throw ProtocolException("frame inválido")
        return ByteArray(n).also { input.readFully(it) }
    }

    /** Bytes já recebidos e ainda não lidos (fila de rede do lado do celular). */
    fun queued(): Int = input.available()

    fun expect(type: String): JSONObject {
        val (t, body) = recv()
        return responseBody(t, body, type)
    }

    override fun request(type: String, body: JSONObject, expect: String): JSONObject {
        send(type, body)
        return expect(expect)
    }

    fun hello(kind: String, name: String, withCert: Boolean): JSONObject {
        val body = JSONObject().put("device_id", id.deviceId).put("name", name).put("kind", kind)
            .put("app_version", BuildConfigVersion).put("protocol", JSONArray().put(1))
        if (withCert) body.put("cert_pem", id.certPem)
        send("hello", body)
        val srv = expect("hello")
        if (srv.optString("device_id") != serverFp) throw ProtocolException("hello do PC não confere com o TLS")
        return srv
    }

    override fun close() = sock.close()
}

const val BuildConfigVersion = "0.1.0"

/** Pareamento completo. `confirm` mostra o código ao usuário e devolve a decisão. Devolve o fingerprint do PC a pinar. */
fun pair(host: String, port: Int, id: Identity, kind: String, name: String, confirm: (code: String, fpS: String) -> Boolean): Pair<String, String> {
    Session(host, port, id, pin = null, presentCert = false).use { s ->
        val pcName = s.hello(kind, name, withCert = true).optString("name")
        val nC = newNonce()
        s.send("pair.request", JSONObject().put("commit", commit(nC, id.deviceId, s.serverFp)))
        val ch = s.expect("pair.challenge")
        val session = ch.getString("session")
        val nS = ch.getString("n_s")
        s.send("pair.reveal", JSONObject().put("session", session).put("n_c", nC))
        val code = sasCode(session, s.serverFp, id.deviceId, nS, nC)
        s.timeoutMs = 70_000 // usuário confirmando nos dois lados (60 s no servidor)
        val ok = confirm(code, s.serverFp)
        s.send("pair.confirm", JSONObject().put("session", session).put("accept", ok))
        if (!ok) throw ProtocolException("recusado no telefone")
        val r = s.expect("pair.result")
        if (!r.optBoolean("ok")) throw ProtocolException("PC recusou: ${r.optString("reason")}")
        return s.serverFp to pcName
    }
}

/** Conexão pareada (mTLS + pin) com um ping; devolve (RTT em ms, nome do PC). */
fun ping(host: String, port: Int, id: Identity, pin: String, kind: String, name: String): Pair<Long, String> {
    Session(host, port, id, pin = pin, presentCert = true).use { s ->
        val pcName = s.hello(kind, name, withCert = false).optString("name")
        val t0 = System.nanoTime()
        s.send("ping")
        s.expect("pong")
        return (System.nanoTime() - t0) / 1_000_000 to pcName
    }
}

data class RemoteAction(val id: String, val description: String, val params: List<String>, val allowed: Boolean = true)

/** Abre uma sessão pareada, faz o hello e executa `block`. */
fun <T> paired(host: String, port: Int, id: Identity, pin: String, kind: String, name: String, block: (Session) -> T): T =
    Session(host, port, id, pin = pin, presentCert = true).use { s ->
        s.hello(kind, name, withCert = false)
        block(s)
    }

fun Pc.listActions(): List<RemoteAction> {
    val arr = request("actions.list", JSONObject(), "actions.result").getJSONArray("actions")
    return List(arr.length()) { i ->
        val a = arr.getJSONObject(i)
        val ps = a.optJSONArray("params") ?: JSONArray()
        RemoteAction(a.getString("id"), a.optString("description"), List(ps.length()) { ps.getJSONObject(it).getString("name") },
            a.optBoolean("allowed", true))
    }
}

/** Resultado em texto; `forbidden` vira instrução de como liberar no PC. */
fun Pc.runAction(action: String, params: Map<String, String>): String {
    val p = JSONObject()
    params.forEach { (k, v) -> p.put(k, v) }
    val r = request("action.run", JSONObject().put("action", action).put("params", p), "action.result")
    return when {
        r.optBoolean("ok") -> "ok" + (r.opt("result")?.takeIf { it != JSONObject.NULL }?.let { ": $it" } ?: "")
        r.optString("error") == "forbidden" -> "Não liberada. No PC: nexus devices allow <id> $action"
        else -> "Erro: ${r.optString("error")}"
    }
}

/** Outro celular ou tablet pareado com o mesmo PC. */
data class Peer(val id: String, val name: String, val connected: Boolean)

fun Pc.listPeers(): List<Peer> {
    val arr = request("devices.peers", JSONObject(), "devices.peers.result").getJSONArray("devices")
    return List(arr.length()) { i -> arr.getJSONObject(i).let { Peer(it.getString("device_id"), it.getString("name"), it.optBoolean("connected")) } }
}

/** Texto (ou um link sozinho) para outro aparelho, pelo PC; o PC pode perguntar antes. Erro → ProtocolException. */
fun Pc.relay(to: String, text: String) {
    val link = text.trim().takeIf { Regex("https?://\\S+", RegexOption.IGNORE_CASE).matches(it) }
    val r = request("relay", JSONObject().put("to", to).put("kind", if (link != null) "url" else "text")
        .put("value", link ?: text.take(65536)), "action.result")
    if (!r.optBoolean("ok")) throw ProtocolException(r.optString("error", "falhou"))
}

fun Pc.shareText(text: String): String {
    return request("share.text", JSONObject().put("text", text), "action.result").optString("result")
}

/** Usa a conexão persistente se estiver de pé; senão abre uma sessão avulsa (que substitui a persistente no PC). */
fun <T> withPc(ctx: android.content.Context, block: (Pc) -> T): T {
    LinkService.connected()?.let { return block(it) }
    val prefs = ctx.getSharedPreferences("nexus", android.content.Context.MODE_PRIVATE)
    val host = prefs.getString("host", null) ?: throw ProtocolException("pareie com o PC primeiro")
    val pin = prefs.getString("pin", null) ?: throw ProtocolException("pareie com o PC primeiro")
    val kind = if (ctx.resources.configuration.smallestScreenWidthDp >= 600) "tablet" else "phone"
    return paired(host, PORT, Identity.load(), pin, kind, android.os.Build.MODEL, block)
}
