package dev.nexus.app

import androidx.compose.foundation.Canvas
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
    private val branco = Color(0xFFF3F4F7)
    private val escuro = Color(0xFF14161B)
    private val cinza = Color(0xFFC9CCD4)
    internal val nexoCorpo = branco
    internal val nexoDormindo = cinza
    internal val nexoOlho = escuro
}

/** O Nexo, nas mesmas coordenadas do design e do notch (viewBox 96×76). */
@Composable
fun Nexo(expr: String, largura: Dp, descricao: String) {
    Canvas(Modifier.size(largura, largura * 76f / 96f).semantics { contentDescription = descricao }) {
        scale(size.width / 96f, size.height / 76f, pivot = Offset.Zero) {
            drawRoundRect(if (expr == "dormindo") Cores.nexoDormindo else Cores.nexoCorpo, Offset(4f, 6f), Size(88f, 64f),
                CornerRadius(26f, 26f))
            val traco = Stroke(5f, cap = StrokeCap.Round)
            fun arco(cx: Float, cy: Float, de: Float, varre: Float) =
                drawArc(Cores.nexoOlho, de, varre, false, Offset(cx - 7f, cy - 7f), Size(14f, 14f), style = traco)
            fun olho(cx: Float, cy: Float, rx: Float, ry: Float, cor: Color = Cores.nexoOlho) =
                drawOval(cor, Offset(cx - rx, cy - ry), Size(rx * 2, ry * 2))
            when (expr) {
                "feito" -> { arco(36f, 45f, 207f, 126f); arco(60f, 45f, 207f, 126f) }
                "curtindo" -> {
                    arco(36f, 33f, 27f, 126f); arco(60f, 33f, 27f, 126f)
                    drawLine(Cores.acento, Offset(78f, 4f), Offset(78f, 16f), 3f, StrokeCap.Round)
                    olho(75f, 17f, 4f, 4f, Cores.acento)
                }
                "atento" -> {
                    olho(36f, 38f, 9f, 9f); olho(60f, 38f, 9f, 9f)
                    olho(39f, 35f, 3f, 3f, Cores.nexoCorpo); olho(63f, 35f, 3f, 3f, Cores.nexoCorpo)
                }
                "surpreso" -> { olho(36f, 36f, 7f, 7f); olho(60f, 36f, 7f, 7f); olho(48f, 54f, 5f, 6f) }
                "dormindo" -> {
                    drawLine(Cores.nexoOlho, Offset(30f, 40f), Offset(42f, 40f), 5f, StrokeCap.Round)
                    drawLine(Cores.nexoOlho, Offset(54f, 40f), Offset(66f, 40f), 5f, StrokeCap.Round)
                    val z = Path().apply { moveTo(74f, 8f); lineTo(82f, 8f); lineTo(74f, 17f); lineTo(82f, 17f) }
                    drawPath(z, Color(0xFF8E929D), style = Stroke(2.4f, cap = StrokeCap.Round, join = StrokeJoin.Round))
                }
                else -> { olho(36f, 38f, 5.5f, 8f); olho(60f, 38f, 5.5f, 8f) }
            }
        }
    }
}

enum class Icone { ESPELHAR, AUDIO, CLIPBOARD, ARQUIVO, AJUSTES }

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
