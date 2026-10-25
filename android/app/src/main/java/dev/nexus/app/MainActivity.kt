package dev.nexus.app

import android.content.ClipboardManager
import android.content.Context
import android.net.Uri
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import java.util.concurrent.CompletableFuture
import kotlin.concurrent.thread

class MainActivity : ComponentActivity() {
    private val prefs by lazy { getSharedPreferences("nexus", Context.MODE_PRIVATE) }
    private var host by mutableStateOf("")
    private var status by mutableStateOf("")
    private var busy by mutableStateOf(false)
    private var pin by mutableStateOf<String?>(null)
    private var pcName by mutableStateOf("PC")
    private var linked by mutableStateOf(false)
    private var linkUp by mutableStateOf(false)
    private var audioOn by mutableStateOf(false)
    private var audioText by mutableStateOf<String?>(null)
    private var settings by mutableStateOf(false)
    private var pending by mutableStateOf<Pair<String, String>?>(null) // código, fp do PC
    private var decision: CompletableFuture<Boolean>? = null
    private var actions by mutableStateOf<List<RemoteAction>>(emptyList())

    private val kind get() = if (resources.configuration.smallestScreenWidthDp >= 600) "tablet" else "phone"
    private val name get() = Build.MODEL

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        host = prefs.getString("host", "") ?: ""
        pin = prefs.getString("pin", null)
        pcName = prefs.getString("pc_name", null) ?: "PC"
        linked = prefs.getBoolean("link", false)
        val tema = darkColorScheme(primary = Cores.acento, onPrimary = Cores.acentoEscuro, background = Cores.fundo,
            surface = Cores.fundo, onSurface = Cores.texto, onBackground = Cores.texto)
        setContent { MaterialTheme(colorScheme = tema) { Surface(Modifier.fillMaxSize(), color = Cores.fundo) { Tela() } } }
    }

    // onStart, não só onCreate: o HyperOS mata o serviço, e voltar ao app deve religá-lo
    override fun onStart() {
        super.onStart()
        if (pin != null && linked) LinkService.start(this)
    }

    // ── trabalho em segundo plano ────────────────────────────────────────
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

    private fun startPair() = work("Pareando…") { id ->
        val (fp, pc) = pair(host.trim(), PORT, id, kind, name) { code, fpS ->
            val f = CompletableFuture<Boolean>()
            runOnUiThread { decision = f; pending = code to fpS }
            f.get()
        }
        prefs.edit().putString("host", host.trim()).putString("pin", fp).putString("pc_name", pc).apply()
        runOnUiThread { pin = fp; pcName = pc; toggleLink(true) }
        "Pareado com $pc"
    }

    private fun startPing() = work("Conectando…") { id ->
        val (ms, pc) = ping(host.trim(), PORT, id, pin!!, kind, name)
        // só grava o IP novo depois que o PC pinado respondeu (mDNS e digitação não são confiáveis)
        prefs.edit().putString("host", host.trim()).putString("pc_name", pc).apply()
        runOnUiThread { pcName = pc }
        if (linked) LinkService.restart(this)
        "Ping ok: $ms ms"
    }

    private fun loadActions() = thread {
        val list = runCatching { withPc(this) { it.listActions() } }.getOrNull()
        runOnUiThread { if (list != null) actions = list }
    }

    private fun runAction(a: RemoteAction) = work("Executando ${a.description}…") {
        "${a.description}: " + withPc(this) { it.runAction(a.id, emptyMap()) }
    }

    private fun mirror() = work("Pedindo ao PC…") {
        val r = withPc(this) { it.request("mirror.request", org.json.JSONObject(), "action.result") }
        if (r.optBoolean("ok")) "Espelhando no PC" else "Espelhar: ${r.optString("error")}"
    }

    private fun sendClipboard() {
        // com o app em primeiro plano o Android deixa ler a área de transferência
        val text = getSystemService(ClipboardManager::class.java).primaryClip
            ?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this)?.toString()
        if (text.isNullOrEmpty()) {
            status = "A área de transferência está vazia"
            return
        }
        work("Enviando…") { if (withPc(this) { it.shareText(text.take(65536)) } == "copiado") "Copiado no PC" else "Enviado ao PC" }
    }

    private fun sendFiles(uris: List<Uri>) {
        if (uris.isEmpty()) return
        work("Enviando ${uris.size} arquivo(s)…") {
            val ok = uris.count { runCatching { FileTransfer.push(this, it) }.isSuccess }
            "$ok de ${uris.size} arquivo(s) no PC"
        }
    }

    private fun toggleAudio() {
        status = when (LinkService.toggleAudio()) {
            null -> "Ligue \"Manter conectado\" primeiro"
            true -> "Recebendo o áudio do PC"
            false -> "Áudio do PC parado"
        }
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
        settings = false
        status = "Pareamento apagado no celular. Revogue também no PC."
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

    // ── telas ────────────────────────────────────────────────────────────
    @Composable
    private fun Tela() {
        LaunchedEffect(pin) {
            while (true) { // estado da conexão persistente e do áudio, que vivem no serviço
                linkUp = LinkService.connected() != null
                audioOn = LinkService.audioPlaying()
                audioText = LinkService.lastAudioText
                delay(1000)
            }
        }
        LaunchedEffect(linkUp) { if (linkUp) loadActions() }
        val p = pending
        when {
            p != null -> Pareamento(p.first, p.second)
            pin == null -> BoasVindas()
            else -> Inicio()
        }
    }

    @Composable
    private fun Cartao(modifier: Modifier = Modifier, conteudo: @Composable () -> Unit) {
        Box(modifier.fillMaxWidth().background(Cores.superficie, RoundedCornerShape(24.dp)).padding(18.dp)) { conteudo() }
    }

    @Composable
    private fun Status() {
        if (status.isNotEmpty())
            Text(status, color = Cores.apagado, fontSize = 13.sp, modifier = Modifier.semantics { contentDescription = status })
    }

    @Composable
    private fun BoasVindas() {
        Column(Modifier.fillMaxSize().statusBarsPadding().padding(22.dp).verticalScroll(rememberScrollState()),
            horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Spacer(Modifier.height(40.dp))
            Nexo("conectado", 150.dp, "Nexo")
            Text("Conecte ao seu PC", fontSize = 24.sp, fontWeight = FontWeight.Bold)
            Text("O PC precisa estar com o Nexus aberto e na mesma rede.", color = Cores.apagado, textAlign = TextAlign.Center)
            OutlinedTextField(host, { host = it }, label = { Text("IP do PC") }, singleLine = true, enabled = !busy,
                modifier = Modifier.fillMaxWidth())
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
                OutlinedButton(::discover, enabled = !busy, modifier = Modifier.weight(1f).height(52.dp)) { Text("Procurar") }
                Button(::startPair, enabled = !busy && host.isNotBlank(), modifier = Modifier.weight(1f).height(52.dp)) {
                    Text("Parear", fontWeight = FontWeight.Bold)
                }
            }
            Status()
        }
    }

    @Composable
    private fun Pareamento(code: String, fp: String) {
        Column(Modifier.fillMaxSize().statusBarsPadding().padding(22.dp),
            horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(18.dp)) {
            Spacer(Modifier.height(40.dp))
            Nexo("atento", 150.dp, "Nexo atento")
            Text("Confira o código", fontSize = 24.sp, fontWeight = FontWeight.Bold)
            Text("O PC mostra um código no notch ou no app Nexus. Confirme só se for igual a este.",
                color = Cores.apagado, textAlign = TextAlign.Center)
            Box(Modifier.background(Cores.superficie, RoundedCornerShape(24.dp)).padding(horizontal = 26.dp, vertical = 22.dp)) {
                Text(code.chunked(3).joinToString(" "), fontSize = 44.sp, fontFamily = FontFamily.Monospace, letterSpacing = 3.sp)
            }
            Text("PC ${fp.take(16)}…", color = Color(0xFF8E929D), fontSize = 13.sp)
            Spacer(Modifier.weight(1f))
            Button({ decision?.complete(true); pending = null }, Modifier.fillMaxWidth().height(54.dp)) {
                Text("É o mesmo código", fontWeight = FontWeight.Bold, fontSize = 16.sp)
            }
            OutlinedButton({ decision?.complete(false); pending = null }, Modifier.fillMaxWidth().height(54.dp)) {
                Text("É diferente — cancelar", fontSize = 16.sp)
            }
        }
    }

    @Composable
    private fun Inicio() {
        val pick = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { sendFiles(it) }
        val expr = when {
            !linkUp -> "dormindo"
            audioOn -> "curtindo"
            else -> "conectado"
        }
        Column(Modifier.fillMaxSize().statusBarsPadding().padding(horizontal = 18.dp, vertical = 16.dp)
            .verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Nexus", fontSize = 26.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                IconButton({ settings = !settings }, Modifier.size(44.dp).background(Cores.superficie, CircleShape)
                    .semantics { contentDescription = "Configurações da conexão" }) {
                    IconeTraco(Icone.AJUSTES, Cores.texto, 20.dp)
                }
            }

            Cartao {
                Column(verticalArrangement = Arrangement.spacedBy(14.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                        Nexo(expr, 76.dp, if (linkUp) "Nexo conectado" else "Nexo dormindo: desconectado")
                        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            Text(pcName, fontSize = 18.sp, fontWeight = FontWeight.Bold)
                            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                                Box(Modifier.size(8.dp).background(if (linkUp) Cores.verde else Color(0xFF6B6F7A), CircleShape))
                                Text(if (linkUp) "Conectado" else if (linked) "Conectando…" else "Desconectado",
                                    color = Color(0xFFB9BCC6), fontSize = 13.sp)
                            }
                        }
                    }
                    Row(Modifier.fillMaxWidth().background(Cores.superficie2, RoundedCornerShape(16.dp))
                        .padding(horizontal = 14.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                        Text("Manter conectado", Modifier.weight(1f))
                        Switch(linked, ::toggleLink, colors = SwitchDefaults.colors(checkedTrackColor = Cores.acento))
                    }
                }
            }

            if (settings) Cartao {
                Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    OutlinedTextField(host, { host = it }, label = { Text("IP do PC") }, singleLine = true, enabled = !busy,
                        modifier = Modifier.fillMaxWidth())
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(::discover, enabled = !busy) { Text("Procurar") }
                        Button(::startPing, enabled = !busy && host.isNotBlank()) { Text("Testar") }
                    }
                    Text("PC ${pin!!.take(16)}…", color = Color(0xFF8E929D), fontSize = 12.sp)
                    OutlinedButton(::forget, enabled = !busy, border = BorderStroke(1.dp, Color(0xFF5A3A36))) {
                        Text("Esquecer este PC", color = Color(0xFFFF9B8E))
                    }
                }
            }

            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Bloco(Icone.ESPELHAR, Cores.azulClaro, "Espelhar no PC", null, false, true, Modifier.weight(1f), ::mirror)
                Bloco(Icone.AUDIO, Cores.azulClaro, "Áudio do PC", if (audioOn) audioText?.removePrefix("Áudio do PC · ") else null,
                    audioOn, linkUp, Modifier.weight(1f), ::toggleAudio)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Bloco(Icone.CLIPBOARD, Cores.verdeClaro, "Enviar área de transferência", null, false, true, Modifier.weight(1f),
                    ::sendClipboard)
                Bloco(Icone.ARQUIVO, Cores.laranja, "Enviar arquivo", null, false, true, Modifier.weight(1f)) { pick.launch(arrayOf("*/*")) }
            }

            if (actions.isNotEmpty()) Cartao { ControlarPc() }
            Status()
        }
    }

    @OptIn(ExperimentalLayoutApi::class)
    @Composable
    private fun ControlarPc() {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Controlar o PC", fontWeight = FontWeight.Bold, fontSize = 15.sp)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                // só ações sem parâmetros viram botão; as não liberadas aparecem esmaecidas em vez de dar erro
                actions.filter { it.params.isEmpty() }.forEach { a ->
                    OutlinedButton({ runAction(a) },
                        enabled = a.allowed && !busy, modifier = Modifier.heightIn(min = 44.dp),
                        border = BorderStroke(1.5.dp, Cores.borda)) {
                        Text(if (a.allowed) a.description else "${a.description} · não liberada", fontSize = 14.sp)
                    }
                }
            }
        }
    }

    @Composable
    private fun Bloco(icone: Icone, corIcone: Color, titulo: String, sub: String?, ativo: Boolean, habilitado: Boolean,
                      modifier: Modifier, acao: () -> Unit) {
        Button(acao, modifier.height(104.dp), enabled = habilitado && !busy, shape = RoundedCornerShape(24.dp),
            colors = ButtonDefaults.buttonColors(containerColor = if (ativo) Cores.acento else Cores.superficie,
                contentColor = if (ativo) Cores.acentoEscuro else Cores.texto,
                disabledContainerColor = Cores.superficie, disabledContentColor = Color(0xFF6B6F7A)),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp)) {
            Column(Modifier.fillMaxSize(), verticalArrangement = Arrangement.SpaceBetween) {
                IconeTraco(icone, if (ativo) Cores.acentoEscuro else if (habilitado) corIcone else Color(0xFF6B6F7A))
                Column {
                    Text(titulo, fontWeight = FontWeight.SemiBold, fontSize = 15.sp, lineHeight = 18.sp)
                    if (sub != null) Text(sub, fontSize = 12.sp)
                }
            }
        }
    }
}
