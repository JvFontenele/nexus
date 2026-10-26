package dev.nexus.app

import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.animation.core.EaseOutCubic
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.keyframes
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.graphics.drawscope.translate
import androidx.compose.ui.input.pointer.pointerInput
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.sin
import kotlin.random.Random
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.scale
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/** Paleta do design (Nexus — notch, app PC e app móvel). */
object Cores {
    val fundo = Color(0xFF0B0C10)
    val superficie = Color(0xFF16181D)
    val superficie2 = Color(0xFF0F1014)
    val borda = Color(0xFF343845)
    val texto = Color(0xFFEDEEF2)
    val apagado = Color(0xFFA7AAB4)
    val acento = Color(0xFF8EA8FF)
    val acentoEscuro = Color(0xFF0E1222)
    val verde = Color(0xFF5BD39A)
    val verdeClaro = Color(0xFF7FE0B0)
    val laranja = Color(0xFFFFC58A)
    val azulClaro = Color(0xFFA9BCFF)
}

/** Mola do notch (forma.js): resposta 0,5 s e amortecimento 0,72; rigidez = (2π/0,5)² ≈ 158. */
fun <T> mola() = spring<T>(dampingRatio = 0.72f, stiffness = 158f)

/** Curva de encolher do notch: cubic-bezier(.45, 0, .2, 1) em 340 ms. */
val curvaEncolher = CubicBezierEasing(0.45f, 0f, 0.2f, 1f)

private val AZUL = Color(0xFF86CFEE)
private val APAGADO = Color(0xFF8AA9B8)
private val PINO = Color(0xFF4FA6CF)
private val ESCURO = Color(0xFF14161F)
private val BOCHECHA = Color(0xFFFF8FA3)
private val NOTA = Color(0xFF8EA8FF)
private val CORACAO = Color(0xFFFF7FA8)
private val ZZ = Color(0xFF999EAD)
private const val BASE = 86f // y da base do corpo: achatar e respirar escalam a partir dela

/**
 * O Nexo, a Gota (fiel ao mascote.js do Balão, viewBox 96×88). Respira, pisca, achata ao aparecer ou mudar de
 * humor, dá um pulinho a cada incremento de [pulos], fica irritado ao toque, tonto com 3 toques rápidos e solta
 * corações no toque longo. Só anima com a janela visível: o relógio de quadros do Compose para em segundo plano.
 */
@Composable
fun Nexo(expr: String, largura: Dp, descricao: String, pulos: Int = 0) {
    val escopo = rememberCoroutineScope()
    var humor by remember { mutableStateOf<Pair<String, Long>?>(null) } // reação passageira (expressão, fim)
    val atual = humor?.first ?: expr
    val relogio by rememberInfiniteTransition("nexo").animateFloat(0f, 3600f,
        infiniteRepeatable(tween(3_600_000, easing = LinearEasing)), "segundos")
    val abertura = remember { Animatable(1f) }
    val sx = remember { Animatable(1f) }
    val sy = remember { Animatable(1f) }
    val pulo = remember { Animatable(0f) }
    val coracoes = remember { Animatable(1900f) }
    val cliques = remember { ArrayList<Long>() }

    fun reagir(e: String, ms: Long) {
        val fim = System.currentTimeMillis() + ms
        humor = e to fim
        escopo.launch { delay(ms); if (humor?.second == fim) humor = null }
    }
    fun achatar() {
        escopo.launch { sy.animateTo(1f, keyframes { durationMillis = 370; 1f at 0 using EaseOutCubic
            0.78f at 70 using EaseOutCubic; 1.1f at 200 using EaseOutCubic }) }
        escopo.launch { sx.animateTo(1f, keyframes { durationMillis = 370; 1f at 0 using EaseOutCubic
            1.16f at 70 using EaseOutCubic; 0.95f at 200 using EaseOutCubic }) }
    }

    LaunchedEffect(Unit) { // piscar: fecha em 70 ms, abre em 130 ms, a cada 2–5 s
        delay(1500 + Random.nextLong(2000))
        while (true) {
            abertura.animateTo(0.06f, tween(70, easing = EaseOutCubic))
            abertura.animateTo(1f, tween(130, easing = EaseOutCubic))
            delay(2000 + Random.nextLong(3000))
        }
    }
    LaunchedEffect(atual) { achatar() }
    val pulosAntes = remember { pulos } // ao abrir a tela não pula pelas transferências de antes
    LaunchedEffect(pulos) {
        if (pulos == pulosAntes) return@LaunchedEffect
        reagir("feito", 1600)
        pulo.snapTo(0f)
        pulo.animateTo(1f, tween(800, easing = LinearEasing))
        pulo.snapTo(0f)
        achatar()
    }

    Canvas(Modifier.size(largura, largura * 88f / 96f)
        .semantics { contentDescription = descricao }
        .pointerInput(Unit) {
            detectTapGestures(
                onTap = {
                    val t = System.currentTimeMillis()
                    cliques.removeAll { t - it > 1700 }
                    cliques += t
                    achatar()
                    if (cliques.size >= 3) { cliques.clear(); reagir("tonto", 2500) } else reagir("irritado", 1200)
                },
                onLongPress = {
                    reagir("feito", 1600)
                    escopo.launch { coracoes.snapTo(0f); coracoes.animateTo(1900f, tween(1900, easing = LinearEasing)) }
                })
        }) {
        val s = relogio
        val dormindo = atual == "dormindo"
        val r = sin(2 * PI.toFloat() * s / (if (dormindo) 4.2f else 3.2f)) * (if (dormindo) 0.035f else 0.022f)
        val tonto = atual == "tonto"
        val h = coracoes.value
        val cs = listOf(0f to 30f, 250f to 66f, 500f to 48f).mapNotNull { (nasce, x) ->
            val a = (h - nasce) / 1400f
            if (a in 0f..<1f) floatArrayOf(x + sin(a * 8) * 2, 26 - a * 22, 7 + a * 3, 1 - a) else null
        }
        desenharNexo(atual, abertura.value, (1 - r * 0.6f) * sx.value, (1 + r) * sy.value,
            9 * abs(sin(2 * PI.toFloat() * pulo.value)), if (tonto) s * 6 else 0f,
            if (tonto) sin(s * 9) * 0.12f else 0f, cs)
    }
}

private fun DrawScope.desenharNexo(expr: String, abertura: Float, sx: Float, sy: Float, pulo: Float, giro: Float,
                                   inclinacao: Float, coracoes: List<FloatArray>) = scale(size.width / 96f, size.height / 88f, Offset.Zero) {
    fun elipse(cx: Float, cy: Float, rx: Float, ry: Float, cor: Color) =
        drawOval(cor, Offset(cx - rx, cy - ry), Size(rx * 2, maxOf(ry, 0.01f) * 2))
    fun linha(x1: Float, y1: Float, x2: Float, y2: Float, w: Float, cor: Color = ESCURO) =
        drawLine(cor, Offset(x1, y1), Offset(x2, y2), w, StrokeCap.Round)
    fun arco(cx: Float, cy: Float, r: Float, de: Float, ate: Float, w: Float) = // ângulos em múltiplos de π, como no Cairo
        drawArc(ESCURO, de * 180, (ate - de) * 180, false, Offset(cx - r, cy - r), Size(r * 2, r * 2),
            style = Stroke(w, cap = StrokeCap.Round))
    fun traco(w: Float) = Stroke(w, cap = StrokeCap.Round, join = StrokeJoin.Round)

    translate(top = -pulo) { rotate(inclinacao * 180 / PI.toFloat(), Offset(48f, BASE)) { scale(sx, sy, Offset(48f, BASE)) {
        // pinos de tomada: o "link" com o celular
        for (x in listOf(39f, 57f)) drawRoundRect(PINO, Offset(x - 2.5f, 9.5f), Size(5f, 14f), CornerRadius(2.5f))
        val corpo = Path().apply {
            moveTo(48f, 20f)
            cubicTo(72f, 20f, 84f, 42f, 84f, 60f)
            cubicTo(84f, 78f, 68f, 86f, 48f, 86f)
            cubicTo(28f, 86f, 12f, 78f, 12f, 60f)
            cubicTo(12f, 42f, 24f, 20f, 48f, 20f)
            close()
        }
        drawPath(corpo, if (expr == "dormindo") APAGADO else AZUL)
        elipse(32f, 40f, 6f, 9f, Color.White.copy(alpha = 0.35f))
        val bochecha = BOCHECHA.copy(alpha = if (expr == "irritado") 0.85f else 0.55f)
        elipse(25f, 68f, 5.5f, 3.3f, bochecha); elipse(71f, 68f, 5.5f, 3.3f, bochecha)

        fun abertos(rx: Float, ry: Float) {
            for (x in listOf(35f, 61f)) elipse(x, 56f, rx, ry * abertura, ESCURO)
            if (abertura > 0.5f) for (x in listOf(35f, 61f)) elipse(x + rx * 0.35f, 56 - ry * 0.4f, rx * 0.32f, rx * 0.32f, Color.White)
        }
        when (expr) {
            "feito", "curtindo" -> { arco(35f, 59f, 6.5f, 1.15f, 1.85f, 4f); arco(61f, 59f, 6.5f, 1.15f, 1.85f, 4f) }
            "dormindo" -> { arco(35f, 55f, 6f, 0.15f, 0.85f, 4f); arco(61f, 55f, 6f, 0.15f, 0.85f, 4f) }
            "tonto" -> for (x in listOf(35f, 61f)) {
                val espiral = Path()
                var a = 0f
                while (a < 4 * PI) {
                    val px = x + 0.6f * a * cos(a + giro); val py = 56 + 0.6f * a * sin(a + giro)
                    if (a == 0f) espiral.moveTo(px, py) else espiral.lineTo(px, py)
                    a += 0.3f
                }
                drawPath(espiral, ESCURO, style = traco(2.4f))
            }
            "atento" -> abertos(8f, 10f)
            "surpreso" -> abertos(6.5f, 6.5f)
            else -> abertos(7f, 9f) // conectado, preocupado, irritado
        }
        if (expr == "preocupado" || expr == "irritado") { // preocupado cai para fora, irritado desce para o meio
            val s = if (expr == "irritado") 1 else -1
            linha(28f, 44f - 2 * s, 40f, 44f + 2 * s, 2.8f); linha(56f, 44f + 2 * s, 68f, 44f - 2 * s, 2.8f)
        }
        when (expr) { // boca
            "feito", "curtindo" -> arco(48f, 66f, 4.5f, 0.2f, 0.8f, 2.6f)
            "surpreso", "atento" -> elipse(48f, 70f, 2.6f, if (expr == "surpreso") 3.4f else 2.2f, ESCURO)
            "preocupado" -> arco(48f, 74f, 4f, 1.25f, 1.75f, 2.6f)
            "irritado" -> linha(44f, 71f, 52f, 71f, 2.6f)
            "tonto" -> drawPath(Path().apply { moveTo(42f, 71f); cubicTo(45f, 68f, 47f, 74f, 50f, 71f)
                cubicTo(52f, 69f, 53f, 70f, 54f, 71f) }, ESCURO, style = traco(2.6f))
            "dormindo" -> elipse(48f, 70f, 1.6f, 1.6f, ESCURO)
            else -> arco(48f, 67.5f, 2.8f, 0.25f, 0.75f, 2.6f)
        }
    } } }

    // enfeites fora do corpo (não achatam)
    if (expr == "curtindo") {
        linha(84f, 6 - pulo, 84f, 18 - pulo, 2.4f, NOTA)
        elipse(81.5f, 19 - pulo, 3f, 2.6f, NOTA)
    } else if (expr == "dormindo") {
        drawPath(Path().apply { moveTo(76f, 8f); lineTo(84f, 8f); lineTo(76f, 17f); lineTo(84f, 17f) }, ZZ, style = traco(2.4f))
    }
    for ((x, y, s, a) in coracoes) drawPath(Path().apply {
        moveTo(x, y + s * 0.3f)
        cubicTo(x, y, x - s / 2, y, x - s / 2, y + s * 0.3f)
        cubicTo(x - s / 2, y + s * 0.6f, x, y + s * 0.8f, x, y + s)
        cubicTo(x, y + s * 0.8f, x + s / 2, y + s * 0.6f, x + s / 2, y + s * 0.3f)
        cubicTo(x + s / 2, y, x, y, x, y + s * 0.3f)
    }, CORACAO.copy(alpha = a))
}

enum class Icone { INICIO, ESPELHAR, AUDIO, CLIPBOARD, ARQUIVO, AJUSTES }

/** Ícones de traço do design (viewBox 24×24), sem depender de biblioteca de ícones. */
@Composable
fun IconeTraco(icone: Icone, cor: Color, tamanho: Dp = 24.dp) {
    Canvas(Modifier.size(tamanho)) {
        scale(size.width / 24f, size.height / 24f, pivot = Offset.Zero) { desenharIcone(icone, cor) }
    }
}

private fun DrawScope.desenharIcone(icone: Icone, cor: Color) {
    val t = Stroke(2f, cap = StrokeCap.Round, join = StrokeJoin.Round)
    fun linha(vararg p: Float) {
        val path = Path().apply { moveTo(p[0], p[1]); for (i in 2 until p.size step 2) lineTo(p[i], p[i + 1]) }
        drawPath(path, cor, style = t)
    }
    when (icone) {
        Icone.INICIO -> {
            linha(3f, 11f, 12f, 4f, 21f, 11f); linha(5f, 9.5f, 5f, 20f, 19f, 20f, 19f, 9.5f); linha(10f, 20f, 10f, 14f, 14f, 14f, 14f, 20f)
        }
        Icone.ESPELHAR -> {
            drawRoundRect(cor, Offset(2f, 4f), Size(20f, 13f), CornerRadius(2f, 2f), style = t)
            linha(8f, 21f, 16f, 21f); linha(12f, 17f, 12f, 21f)
        }
        Icone.AUDIO -> {
            linha(11f, 5f, 6f, 9f, 3f, 9f, 3f, 15f, 6f, 15f, 11f, 19f, 11f, 5f)
            drawArc(cor, -45f, 90f, false, Offset(10.5f, 7f), Size(10f, 10f), style = t)
        }
        Icone.CLIPBOARD -> {
            drawRoundRect(cor, Offset(8f, 3f), Size(8f, 4f), CornerRadius(1f, 1f), style = t)
            linha(8f, 5f, 6f, 5f, 4f, 7f, 4f, 19f, 6f, 21f, 18f, 21f, 20f, 19f, 20f, 7f, 18f, 5f, 16f, 5f)
        }
        Icone.ARQUIVO -> {
            linha(14f, 3f, 6f, 3f, 4f, 5f, 4f, 19f, 6f, 21f, 18f, 21f, 20f, 19f, 20f, 9f, 14f, 3f)
            linha(14f, 3f, 14f, 9f, 20f, 9f); linha(12f, 18f, 12f, 12f); linha(9f, 15f, 12f, 12f, 15f, 15f)
        }
        Icone.AJUSTES -> {
            drawCircle(cor, 3f, Offset(12f, 12f), style = t)
            linha(12f, 2f, 12f, 5f); linha(12f, 19f, 12f, 22f); linha(2f, 12f, 5f, 12f); linha(19f, 12f, 22f, 12f)
            linha(4.9f, 4.9f, 7f, 7f); linha(17f, 17f, 19.1f, 19.1f); linha(4.9f, 19.1f, 7f, 17f); linha(17f, 7f, 19.1f, 4.9f)
        }
    }
}
