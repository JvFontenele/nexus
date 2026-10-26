package dev.nexus.app

import android.app.DownloadManager
import android.content.ClipboardManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.input.InputManager
import android.media.projection.MediaProjectionConfig
import android.media.projection.MediaProjectionManager
import android.net.Uri
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateContentSize
import androidx.compose.animation.core.tween
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.shrinkVertically
import androidx.compose.animation.togetherWith
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
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
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
import java.util.concurrent.CompletableFuture
import kotlin.concurrent.thread
import kotlinx.coroutines.delay

private val ABAS = listOf("Início" to Icone.INICIO, "Áudio" to Icone.AUDIO, "Arquivos" to Icone.ARQUIVO, "Ajustes" to Icone.AJUSTES)

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
    private var audioOutOn by mutableStateOf(false)
    private var audioOutText by mutableStateOf<String?>(null)
    private val pedirCaptura = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
        val data = r.data
        status = if (r.resultCode != RESULT_OK || data == null) "Áudio para o PC: cancelado"
        else when (runCatching { LinkService.startAudioOut(r.resultCode, data) }.getOrElse { status = "Erro: ${it.message}"; return@registerForActivityResult }) {
            null -> "Ligue \"Manter conectado\" primeiro"
            else -> "Conectando o áudio ao PC…"
        }
    }
    private val pedirMicrofone = registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        if (ok) pedirConfirmacao() else status = "Sem a permissão de áudio, o Android não deixa capturar o som dos apps"
    }
    private var aba by mutableStateOf(0) // Início, Áudio, Arquivos, Ajustes
    private var pending by mutableStateOf<Pair<String, String>?>(null) // código, fp do PC
    private var decision: CompletableFuture<Boolean>? = null
    private var actions by mutableStateOf<List<RemoteAction>>(emptyList())
    private var peers by mutableStateOf<List<Peer>>(emptyList())
    private var alvo: Peer? = null // aparelho escolhido para o seletor de arquivos
    private var controlado by mutableStateOf(false)
    private var autoclique by mutableStateOf(false)
    private var bateriaLivre by mutableStateOf(true)
    private var tecladoBt by mutableStateOf(false)
    private var layoutEscolhido by mutableStateOf(false)

    private val kind get() = if (resources.configuration.smallestScreenWidthDp >= 600) "tablet" else "phone"
    private val name get() = Build.MODEL

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        host = prefs.getString("host", "") ?: ""
        pin = prefs.getString("pin", null)
        pcName = prefs.getString("pc_name", null) ?: "PC"
        linked = prefs.getBoolean("link", false)
        layoutEscolhido = prefs.getBoolean("layout_teclado_ok", false)
        val tema = darkColorScheme(primary = Cores.acento, onPrimary = Cores.acentoEscuro, background = Cores.fundo,
            surface = Cores.fundo, onSurface = Cores.texto, onBackground = Cores.texto)
        setContent { MaterialTheme(colorScheme = tema) { Surface(Modifier.fillMaxSize(), color = Cores.fundo) { Tela() } } }
        pedidoDoPc(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        pedidoDoPc(intent)
    }

    /** Toque na notificação "O PC pediu o áudio deste aparelho". */
    private fun pedidoDoPc(intent: Intent?) {
        if (intent?.action != LinkService.ACTION_MANDAR_AUDIO) return
        intent.action = null // girar a tela não pede de novo
        aba = 1
        if (!LinkService.audioSending()) toggleAudioOut()
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

    private fun loadPeers() = thread {
        val list = runCatching { withPc(this) { it.listPeers() } }.getOrNull()
        runOnUiThread { if (list != null) peers = list }
    }

    /** Área de transferência para outro aparelho (com a tela em primeiro plano o Android deixa ler). */
    private fun relayClipboard(p: Peer) {
        val text = getSystemService(ClipboardManager::class.java).primaryClip
            ?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this)?.toString()
        if (text.isNullOrEmpty()) {
            status = "A área de transferência está vazia"
            return
        }
        work("Enviando ao ${p.name}… (o PC pode perguntar antes)") { withPc(this) { it.relay(p.id, text) }; "Enviado ao ${p.name}" }
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

    private fun sendFiles(uris: List<Uri>, to: Peer? = null) {
        if (uris.isEmpty()) return
        work("Enviando ${uris.size} arquivo(s)…") {
            var erro: Throwable? = null
            val ok = uris.count { runCatching { FileTransfer.push(this, it, to) }.onFailure { e -> erro = e }.isSuccess }
            "$ok de ${uris.size} arquivo(s) no ${to?.name ?: "PC"}" + (erro?.let { " · ${it.message}" } ?: "")
        }
    }

    private fun toggleAudio() {
        status = when (LinkService.toggleAudio()) {
            null -> "Ligue \"Manter conectado\" primeiro"
            true -> "Recebendo o áudio do PC"
            false -> "Áudio do PC parado"
        }
    }

    private fun toggleAudioOut() {
        when {
            LinkService.audioSending() -> { LinkService.stopAudioOut(); status = "Áudio para o PC parado" }
            LinkService.connected() == null -> status = "Ligue \"Manter conectado\" primeiro"
            checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED -> pedirConfirmacao()
            else -> pedirMicrofone.launch(android.Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun pedirConfirmacao() {
        val mpm = getSystemService(MediaProjectionManager::class.java)
        // tela inteira: no Android 14+, "um app só" também serviria, mas pergunta qual app a cada vez
        pedirCaptura.launch(if (Build.VERSION.SDK_INT >= 34) mpm.createScreenCaptureIntent(MediaProjectionConfig.createConfigForDefaultDisplay())
            else mpm.createScreenCaptureIntent())
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
        aba = 0
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

    /** Abre a primeira tela de configuração que existir neste aparelho; senão, as informações do app. */
    private fun abrir(vararg intents: Intent) {
        val app = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))
        for (i in listOf(*intents, app)) {
            try { startActivity(i); return } catch (_: Exception) {} // ActivityNotFound ou SecurityException da ROM
        }
    }

    private fun pedirBateria() = abrir(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
        Uri.parse("package:$packageName")))

    private fun componente(pkg: String, cls: String) = Intent().setComponent(ComponentName(pkg, cls))

    // ── telas ────────────────────────────────────────────────────────────
    @Composable
    private fun Tela() {
        LaunchedEffect(pin) {
            while (true) { // estado da conexão persistente e do áudio, que vivem no serviço
                linkUp = LinkService.connected() != null
                audioOn = LinkService.audioPlaying()
                audioText = LinkService.lastAudioText
                audioOutOn = LinkService.audioSending()
                audioOutText = LinkService.lastAudioOutText
                autoclique = LinkService.cliqueAutomatico(this@MainActivity)
                bateriaLivre = getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName)
                delay(1000)
            }
        }
        LaunchedEffect(linkUp) { if (linkUp) loadActions() }
        LaunchedEffect(linkUp) { while (linkUp) { loadPeers(); delay(15_000) } } // quem está conectado muda
        DisposableEffect(Unit) { // teclado/mouse do PC: UHID aparece e some com a borda
            val im = getSystemService(InputManager::class.java)
            val l = object : InputManager.InputDeviceListener {
                override fun onInputDeviceAdded(id: Int) {
                    controlado = LinkService.pcControlando(this@MainActivity)
                    tecladoBt = LinkService.tecladoBluetooth(this@MainActivity)
                }
                override fun onInputDeviceRemoved(id: Int) = onInputDeviceAdded(id)
                override fun onInputDeviceChanged(id: Int) = onInputDeviceAdded(id)
            }
            l.onInputDeviceAdded(0)
            im.registerInputDeviceListener(l, null)
            onDispose { im.unregisterInputDeviceListener(l) }
        }
        val p = pending
        when {
            p != null -> Pareamento(p.first, p.second)
            pin == null -> BoasVindas()
            else -> Principal()
        }
    }

    @Composable
    private fun Cartao(modifier: Modifier = Modifier, conteudo: @Composable () -> Unit) {
        Box(modifier.fillMaxWidth().background(Cores.superficie, RoundedCornerShape(28.dp)).animateContentSize(mola())
            .padding(18.dp)) { conteudo() }
    }

    @Composable
    private fun Status() {
        // troca de conteúdo como no notch: some rápido, o novo entra com fade e escala 0,97→1 depois de 160 ms
        AnimatedContent(status, transitionSpec = {
            (fadeIn(tween(220, delayMillis = 160)) + scaleIn(mola(), initialScale = 0.97f)) togetherWith fadeOut(tween(160))
        }, label = "status") { st ->
            if (st.isNotEmpty())
                Text(st, color = Cores.apagado, fontSize = 13.sp, modifier = Modifier.semantics { contentDescription = st })
        }
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
    private fun Principal() {
        val pick = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { sendFiles(it) }
        val escolher = { pick.launch(arrayOf("*/*")) }
        val pickPara = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
            alvo?.let { sendFiles(uris, it) }
        }
        val rolagem = rememberScrollState()
        LaunchedEffect(aba) { rolagem.scrollTo(0) }
        Column(Modifier.fillMaxSize()) {
            Column(Modifier.weight(1f).fillMaxWidth().statusBarsPadding().padding(horizontal = 18.dp, vertical = 16.dp)
                .verticalScroll(rolagem), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                Text(if (aba == 0) "Nexus" else ABAS[aba].first, fontSize = 26.sp, fontWeight = FontWeight.Bold)
                when (aba) {
                    0 -> AbaInicio(escolher) { p -> alvo = p; pickPara.launch(arrayOf("*/*")) }
                    1 -> AbaAudio()
                    2 -> AbaArquivos(escolher)
                    else -> AbaAjustes()
                }
                Status()
            }
            NavigationBar(containerColor = Cores.superficie) {
                ABAS.forEachIndexed { i, (nome, icone) ->
                    NavigationBarItem(aba == i, { aba = i }, icon = { IconeTraco(icone, if (aba == i) Cores.acentoEscuro else Cores.apagado) },
                        label = { Text(nome) }, colors = NavigationBarItemDefaults.colors(indicatorColor = Cores.acento,
                            selectedTextColor = Cores.texto, unselectedTextColor = Cores.apagado))
                }
            }
        }
    }

    @Composable
    private fun AbaInicio(escolher: () -> Unit, arquivoPara: (Peer) -> Unit) {
        val expr = when {
            !linked -> "dormindo"
            !linkUp -> "preocupado"
            audioOn || audioOutOn -> "curtindo"
            else -> "conectado"
        }
        Cartao {
            Column(verticalArrangement = Arrangement.spacedBy(14.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                    Nexo(expr, 76.dp, if (linkUp) "Nexo conectado" else "Nexo $expr: desconectado", FileTransfer.concluidas)
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
                if (linked && !bateriaLivre) OutlinedButton({ aba = 3 }, Modifier.fillMaxWidth().heightIn(min = 44.dp),
                    border = BorderStroke(1.5.dp, Cores.borda)) {
                    Text("Manter o Nexus ligado · falta liberar", fontSize = 14.sp)
                }
            }
        }
        Aparece(controlado) { Cartao { Controle() } }
        Aparece(tecladoBt && !layoutEscolhido) { Cartao { LayoutTeclado() } }
        FileTransfer.ativas.forEach { t -> key(t.id) { Cartao { Transferindo(t) } } }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Bloco(Icone.ESPELHAR, Cores.azulClaro, "Espelhar no PC", null, false, true, Modifier.weight(1f), ::mirror)
            Bloco(Icone.ARQUIVO, Cores.laranja, "Enviar arquivo", null, false, true, Modifier.weight(1f), escolher)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Bloco(Icone.CLIPBOARD, Cores.verdeClaro, "Enviar área de transferência", null, false, true, Modifier.weight(1f),
                ::sendClipboard)
        }
        if (actions.isNotEmpty()) Cartao { ControlarPc() }
        if (linkUp && peers.isNotEmpty()) Cartao { OutrosAparelhos(arquivoPara) }
    }

    /** Mandar a outro celular ou tablet pareado com o mesmo PC; o PC repassa (e pode perguntar antes). */
    @Composable
    private fun OutrosAparelhos(arquivoPara: (Peer) -> Unit) {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("Outros aparelhos", fontWeight = FontWeight.Bold, fontSize = 15.sp)
            peers.forEach { p ->
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Box(Modifier.size(8.dp).background(if (p.connected) Cores.verde else Color(0xFF6B6F7A), CircleShape))
                        Text(p.name, fontWeight = FontWeight.Bold)
                        Text(if (p.connected) "conectado" else "desconectado", color = Cores.apagado, fontSize = 13.sp)
                    }
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton({ relayClipboard(p) }, enabled = p.connected && !busy, modifier = Modifier.heightIn(min = 44.dp),
                            border = BorderStroke(1.5.dp, Cores.borda)) { Text("Área de transferência", fontSize = 14.sp) }
                        OutlinedButton({ arquivoPara(p) }, enabled = p.connected && !busy, modifier = Modifier.heightIn(min = 44.dp),
                            border = BorderStroke(1.5.dp, Cores.borda)) { Text("Arquivo", fontSize = 14.sp) }
                    }
                }
            }
        }
    }

    @Composable
    private fun AbaAudio() {
        CartaoAudio("Áudio do PC aqui", "O som do PC toca neste aparelho.",
            if (audioOn) audioText?.removePrefix("Áudio do PC · ") else null, audioOn, linkUp, ::toggleAudio)
        CartaoAudio("Áudio daqui no PC", "O som dos apps deste aparelho toca no PC. O Android pede confirmação a cada vez.",
            if (audioOutOn) "Tocando no PC" else audioOutText, audioOutOn, linkUp || audioOutOn, ::toggleAudioOut)
        Text(if (linkUp) "Baixe o volume deste aparelho: o PC continua tocando."
            else "Ligue \"Manter conectado\" no Início para usar o áudio.", color = Cores.apagado, fontSize = 13.sp)
    }

    @Composable
    private fun CartaoAudio(titulo: String, explica: String, sub: String?, ativo: Boolean, habilitado: Boolean, acao: () -> Unit) {
        Cartao {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                IconeTraco(Icone.AUDIO, if (ativo) Cores.acento else if (habilitado) Cores.azulClaro else Color(0xFF6B6F7A), 30.dp)
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(titulo, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                    Text(explica, color = Cores.apagado, fontSize = 13.sp)
                    if (sub != null) Text(sub, color = if (ativo) Cores.verde else Cores.laranja, fontSize = 13.sp)
                }
                Switch(ativo, { acao() }, enabled = habilitado && !busy,
                    colors = SwitchDefaults.colors(checkedTrackColor = Cores.acento),
                    modifier = Modifier.semantics { contentDescription = titulo })
            }
        }
    }

    @Composable
    private fun AbaArquivos(escolher: () -> Unit) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Bloco(Icone.ARQUIVO, Cores.laranja, "Enviar arquivo", null, false, true, Modifier.weight(1f), escolher)
            Bloco(Icone.CLIPBOARD, Cores.verdeClaro, "Enviar área de transferência", null, false, true, Modifier.weight(1f),
                ::sendClipboard)
        }
        FileTransfer.ativas.forEach { t -> key(t.id) { Cartao { Transferindo(t) } } }
        Cartao {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("Recebidos do PC", fontWeight = FontWeight.Bold, fontSize = 15.sp)
                Text("Ficam em Download/Nexus.", color = Cores.apagado, fontSize = 13.sp)
                OutlinedButton({ abrir(Intent(DownloadManager.ACTION_VIEW_DOWNLOADS)) },
                    border = BorderStroke(1.5.dp, Cores.borda)) { Text("Abrir recebidos") }
            }
        }
    }

    @Composable
    private fun AbaAjustes() {
        Cartao {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("PC pareado: $pcName", fontWeight = FontWeight.Bold, fontSize = 15.sp)
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
        Cartao { ManterLigado() }
        Cartao { LayoutTeclado() }
        Cartao {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("Sobre", fontWeight = FontWeight.Bold, fontSize = 15.sp)
                Text("Nexus ${runCatching { packageManager.getPackageInfo(packageName, 0).versionName }.getOrNull() ?: ""}",
                    fontSize = 14.sp)
                Text("Integração PC ↔ Android, cifrada e sem nuvem. Software livre, GPL-3.0.", color = Cores.apagado,
                    fontSize = 13.sp)
            }
        }
    }

    /** Entra crescendo com a mola do notch; sai encolhendo com a curva de fechar (340 ms). */
    @Composable
    private fun Aparece(visivel: Boolean, conteudo: @Composable () -> Unit) {
        AnimatedVisibility(visivel,
            enter = expandVertically(mola()) + fadeIn(tween(220, delayMillis = 160)) + scaleIn(mola(), initialScale = 0.97f),
            exit = shrinkVertically(tween(340, easing = curvaEncolher)) + fadeOut(tween(160))) { conteudo() }
    }

    @Composable
    private fun Controle() {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("O PC está controlando este celular", fontWeight = FontWeight.Bold, fontSize = 15.sp)
            Text("O teclado e o mouse do PC estão ligados aqui.", color = Cores.apagado, fontSize = 13.sp)
            if (autoclique) {
                Text("O clique automático do Android está ligado: o celular vai clicar sozinho quando o ponteiro parar.",
                    color = Cores.laranja, fontSize = 14.sp)
                OutlinedButton({ abrir(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) },
                    border = BorderStroke(1.5.dp, Cores.borda)) { Text("Abrir Acessibilidade") }
            }
        }
    }

    // shortcut: não há API pública para ler o layout escolhido de um teclado físico; o "Já escolhi" fica salvo
    @Composable
    private fun LayoutTeclado() {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Escolha Português (Brasil) para o teclado Nexus", fontWeight = FontWeight.Bold, fontSize = 15.sp)
            Text("Sem isso, o Android usa o inglês (EUA) e o ç e alguns símbolos saem errados.",
                color = Cores.apagado, fontSize = 13.sp)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button({ abrir(Intent(Settings.ACTION_HARD_KEYBOARD_SETTINGS), Intent(Settings.ACTION_INPUT_METHOD_SETTINGS)) }) {
                    Text("Abrir teclado físico")
                }
                OutlinedButton({ layoutEscolhido = true; prefs.edit().putBoolean("layout_teclado_ok", true).apply() },
                    border = BorderStroke(1.5.dp, Cores.borda)) { Text("Já escolhi") }
            }
        }
    }

    @Composable
    private fun Transferindo(t: FileTransfer.Progresso) {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(t.titulo, fontWeight = FontWeight.Bold, fontSize = 15.sp)
            Text("${t.nome} · ${t.pct}%", color = Cores.apagado, fontSize = 13.sp)
            LinearProgressIndicator({ t.pct / 100f }, Modifier.fillMaxWidth(), color = Cores.acento,
                trackColor = Cores.superficie2)
            OutlinedButton({ t.cancelada = true }, border = BorderStroke(1.dp, Color(0xFF5A3A36))) {
                Text("Cancelar", color = Color(0xFFFF9B8E))
            }
        }
    }

    @Composable
    private fun ManterLigado() {
        val fab = Build.MANUFACTURER.lowercase()
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Manter o Nexus ligado", fontWeight = FontWeight.Bold, fontSize = 15.sp)
            Text("Para receber textos, arquivos e o áudio do PC com a tela apagada, o Android precisa deixar o Nexus " +
                "rodando. Ele só mantém uma conexão cifrada com o seu PC e manda um sinal a cada 30 s. Ao ligar o " +
                "celular, o Nexus volta sozinho.", color = Cores.apagado, fontSize = 13.sp)
            if (bateriaLivre) Text("Otimização de bateria: liberada", color = Cores.verde, fontSize = 14.sp)
            else Button(::pedirBateria) { Text("Liberar da otimização de bateria") }
            when {
                fab in setOf("xiaomi", "redmi", "poco") -> {
                    Text("No HyperOS: ligue a Inicialização automática do Nexus e, em Economia de bateria, escolha " +
                        "\"Sem restrições\".", fontSize = 13.sp)
                    OutlinedButton({ abrir(componente("com.miui.securitycenter",
                        "com.miui.permcenter.autostart.AutoStartManagementActivity")) },
                        border = BorderStroke(1.5.dp, Cores.borda)) { Text("Inicialização automática") }
                    OutlinedButton({ abrir(componente("com.miui.powerkeeper", "com.miui.powerkeeper.ui.HiddenAppsConfigActivity")
                        .putExtra("package_name", packageName).putExtra("package_label", "Nexus")) },
                        border = BorderStroke(1.5.dp, Cores.borda)) { Text("Economia de bateria") }
                }
                fab == "samsung" -> {
                    Text("Na One UI: em Bateria, escolha \"Sem restrições\", e tire o Nexus de \"Apps em suspensão\" " +
                        "(Cuidados com o dispositivo → Bateria → Limites de uso em segundo plano).", fontSize = 13.sp)
                    OutlinedButton({ abrir() }, border = BorderStroke(1.5.dp, Cores.borda)) { Text("Bateria do Nexus") }
                    OutlinedButton({ abrir(componente("com.samsung.android.lool",
                        "com.samsung.android.sm.battery.ui.BatteryActivity")) },
                        border = BorderStroke(1.5.dp, Cores.borda)) { Text("Limites em segundo plano") }
                }
                else -> {
                    Text("Se o sistema tiver \"Inicialização automática\" ou restrição de bateria por app, libere o Nexus.",
                        fontSize = 13.sp)
                    OutlinedButton({ abrir() }, border = BorderStroke(1.5.dp, Cores.borda)) { Text("Informações do app") }
                }
            }
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
