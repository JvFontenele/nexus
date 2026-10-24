package dev.nexus.app

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.ui.Alignment
import androidx.compose.material3.Text
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.util.concurrent.CompletableFuture
import kotlin.concurrent.thread

class MainActivity : ComponentActivity() {
    private val prefs by lazy { getSharedPreferences("nexus", Context.MODE_PRIVATE) }
    private var host by mutableStateOf("")
    private var status by mutableStateOf("")
    private var busy by mutableStateOf(false)
    private var pin by mutableStateOf<String?>(null)
    private var linked by mutableStateOf(false)
    private var pending by mutableStateOf<Pair<String, String>?>(null) // código, fp do PC
    private var decision: CompletableFuture<Boolean>? = null
    private var actions by mutableStateOf<List<RemoteAction>>(emptyList())
    private val inputs = mutableStateMapOf<String, String>() // "acao/param" -> valor

    private val kind get() = if (resources.configuration.smallestScreenWidthDp >= 600) "tablet" else "phone"
    private val name get() = Build.MODEL

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        host = prefs.getString("host", "") ?: ""
        pin = prefs.getString("pin", null)
        linked = prefs.getBoolean("link", false)
        setContent { MaterialTheme { Surface(Modifier.fillMaxSize()) { Screen() } } }
    }

    // onStart, não só onCreate: o HyperOS mata o serviço, e voltar ao app deve religá-lo
    override fun onStart() {
        super.onStart()
        if (pin != null && linked) LinkService.start(this)
    }

    private fun work(label: String, block: (Identity) -> String) {
        busy = true
        status = label
        thread {
            val msg = try {
                block(Identity.load())
            } catch (e: Exception) {
                "Erro: ${e.message ?: e.javaClass.simpleName}"
            }
            runOnUiThread { status = msg; busy = false; pending = null }
        }
    }

    private fun startPair() = work("Pareando… confira o código no PC") { id ->
        val fp = pair(host.trim(), PORT, id, kind, name) { code, fpS ->
            val f = CompletableFuture<Boolean>()
            runOnUiThread { decision = f; pending = code to fpS }
            f.get()
        }
        prefs.edit().putString("host", host.trim()).putString("pin", fp).apply()
        runOnUiThread { pin = fp }
        "Pareado com ${fp.take(16)}…"
    }

    private fun startPing() = work("Conectando…") { id ->
        val ms = ping(host.trim(), PORT, id, pin!!, kind, name)
        // só grava o IP novo depois que o PC pinado respondeu (mDNS e digitação não são confiáveis)
        prefs.edit().putString("host", host.trim()).apply()
        if (linked) LinkService.restart(this)
        "Ping ok: $ms ms"
    }

    private fun loadActions() = work("Carregando ações…") { id ->
        val list = withPc(this) { it.listActions() }
        runOnUiThread { actions = list }
        "${list.size} ações disponíveis no PC"
    }

    private fun runAction(a: RemoteAction) = work("Executando ${a.id}…") { id ->
        val params = a.params.associateWith { inputs["${a.id}/$it"].orEmpty() }
        "${a.id}: " + withPc(this) { it.runAction(a.id, params) }
    }

    private fun toggleLink(on: Boolean) {
        linked = on
        prefs.edit().putBoolean("link", on).apply()
        if (on) {
            requestPermissions(arrayOf(android.Manifest.permission.POST_NOTIFICATIONS), 1)
            LinkService.start(this)
        } else LinkService.stop(this)
    }

    private fun forget() {
        toggleLink(false)
        prefs.edit().remove("pin").apply()
        pin = null
        actions = emptyList()
        status = "Pareamento apagado no telefone. Revogue também no PC: nexus devices revoke"
    }

    /** Procura `_nexus._tcp`. O resultado só preenche o campo: identidade vem do SAS/pin, nunca do mDNS. */
    private fun discover() {
        val nsd = getSystemService(NsdManager::class.java)
        status = "Procurando na rede…"
        val listener = object : NsdManager.DiscoveryListener {
            override fun onServiceFound(info: NsdServiceInfo) {
                nsd.stopServiceDiscovery(this)
                @Suppress("DEPRECATION")
                nsd.resolveService(info, object : NsdManager.ResolveListener {
                    override fun onServiceResolved(r: NsdServiceInfo) {
                        @Suppress("DEPRECATION")
                        val ip = r.host?.hostAddress ?: return
                        runOnUiThread { host = ip; status = "Encontrado: ${r.serviceName} ($ip)" }
                    }
                    override fun onResolveFailed(r: NsdServiceInfo, code: Int) {
                        runOnUiThread { status = "Falha ao resolver ($code)" }
                    }
                })
            }
            override fun onDiscoveryStarted(t: String) {}
            override fun onDiscoveryStopped(t: String) {}
            override fun onServiceLost(info: NsdServiceInfo) {}
            override fun onStartDiscoveryFailed(t: String, code: Int) { runOnUiThread { status = "Busca falhou ($code)" } }
            override fun onStopDiscoveryFailed(t: String, code: Int) {}
        }
        nsd.discoverServices("_nexus._tcp", NsdManager.PROTOCOL_DNS_SD, listener)
    }

    @androidx.compose.runtime.Composable
    private fun Screen() {
        Column(Modifier.padding(24.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("Nexus", style = MaterialTheme.typography.headlineMedium)
            OutlinedTextField(host, { host = it }, label = { Text("IP do PC") }, singleLine = true, enabled = !busy)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(::discover, enabled = !busy) { Text("Procurar") }
                if (pin == null) Button(::startPair, enabled = !busy && host.isNotBlank()) { Text("Parear") }
                else Button(::startPing, enabled = !busy && host.isNotBlank()) { Text("Ping") }
            }
            pending?.let { (code, fp) ->
                Text("Código", style = MaterialTheme.typography.labelLarge)
                Text(code.chunked(3).joinToString(" "), fontSize = 40.sp)
                Text("PC: ${fp.take(16)}…\nConfirme só se o PC mostrar o mesmo código.")
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button({ decision?.complete(true); pending = null }) { Text("Confirmar") }
                    OutlinedButton({ decision?.complete(false); pending = null }) { Text("Recusar") }
                }
            }
            Text(status)
            if (pin != null) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Switch(linked, ::toggleLink)
                    Text("Manter conectado (receber do PC)")
                }
                OutlinedButton({
                    status = when (LinkService.toggleAudio()) {
                        null -> "Ligue \"Manter conectado\" primeiro"
                        true -> "Recebendo o áudio do PC (veja a notificação)"
                        false -> "Áudio do PC parado"
                    }
                }) { Text("Áudio do PC aqui") }
                OutlinedButton(::loadActions, enabled = !busy && host.isNotBlank()) { Text("Ações do PC") }
                actions.forEach { a ->
                    Text(a.description.ifBlank { a.id }, style = MaterialTheme.typography.titleSmall)
                    a.params.forEach { p ->
                        OutlinedTextField(inputs["${a.id}/$p"].orEmpty(), { inputs["${a.id}/$p"] = it },
                            label = { Text(p) }, singleLine = true, enabled = !busy)
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button({ runAction(a) }, enabled = !busy) { Text("Executar") }
                        if (a.params.isEmpty()) OutlinedButton({ ActionTile.choose(this@MainActivity, a); status = "Bloco rápido: ${a.description}" }) {
                            Text("No bloco rápido")
                        }
                    }
                }
                Text("Pareado com ${pin!!.take(16)}…", style = MaterialTheme.typography.bodySmall)
                OutlinedButton(::forget, enabled = !busy) { Text("Esquecer PC") }
            }
        }
    }
}
