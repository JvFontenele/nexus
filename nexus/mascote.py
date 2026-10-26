"""O Nexo (mascote): gota azul-mar com dois pinos de tomada, em Cairo.

Mesmo desenho e mesmas coordenadas (viewBox 96×88) do `mascote.js` do Balão; aqui o app GTK anima só piscar,
olhar e respirar (`Animacao`).
"""
import math
import random

AZUL = (0.525, 0.812, 0.933)  # #86CFEE
APAGADO = (0.541, 0.663, 0.722)  # #8AA9B8 (dormindo)
PINO = (0.31, 0.651, 0.812)  # #4FA6CF
ESCURO = (0.078, 0.086, 0.122)
BOCHECHA = (1.0, 0.561, 0.639)
NOTA = (0.557, 0.659, 1.0)
ZZ = (0.6, 0.62, 0.68)
LARGURA, ALTURA, BASE = 96, 88, 86
EXPRESSOES = ("conectado", "atento", "feito", "curtindo", "surpreso", "preocupado", "irritado", "tonto", "dormindo")


def _elipse(cr, cx, cy, rx, ry):
    cr.save()
    cr.translate(cx, cy)
    cr.scale(rx, max(ry, 0.01))
    cr.new_sub_path()
    cr.arc(0, 0, 1, 0, 2 * math.pi)
    cr.restore()


def _linha(cr, x1, y1, x2, y2):
    cr.new_path()
    cr.move_to(x1, y1)
    cr.line_to(x2, y2)
    cr.stroke()


def _arco(cr, cx, cy, r, de, ate):
    cr.new_path()
    cr.arc(cx, cy, r, de * math.pi, ate * math.pi)
    cr.stroke()


def desenhar(cr, largura: float, altura: float, expr: str, olhar=(0.0, 0.0), abertura: float = 1.0,
             respiro: float = 0.0):
    """olhar: (dx, dy) de -1 a 1; abertura: 0 (fechado) a 1; respiro: deformação vertical (+ estica, - achata)."""
    escala = min(largura / LARGURA, altura / ALTURA)
    cr.save()
    cr.translate((largura - LARGURA * escala) / 2, (altura - ALTURA * escala) / 2)
    cr.scale(escala, escala)
    cr.set_line_cap(1)  # ROUND

    cr.save()
    cr.translate(48, BASE)
    cr.scale(1 - respiro * 0.6, 1 + respiro)
    cr.translate(-48, -BASE)
    cr.set_source_rgb(*PINO)
    for x in (39, 57):
        cr.new_path()
        cr.arc(x, 12, 2.5, math.pi, 0)
        cr.arc(x, 21, 2.5, 0, math.pi)
        cr.close_path()
        cr.fill()
    cr.new_path()
    cr.move_to(48, 20)
    cr.curve_to(72, 20, 84, 42, 84, 60)
    cr.curve_to(84, 78, 68, 86, 48, 86)
    cr.curve_to(28, 86, 12, 78, 12, 60)
    cr.curve_to(12, 42, 24, 20, 48, 20)
    cr.close_path()
    cr.set_source_rgb(*(APAGADO if expr == "dormindo" else AZUL))
    cr.fill()
    cr.set_source_rgba(1, 1, 1, 0.35)
    _elipse(cr, 32, 40, 6, 9)
    cr.fill()
    cr.set_source_rgba(*BOCHECHA, 0.85 if expr == "irritado" else 0.55)
    _elipse(cr, 25, 68, 5.5, 3.3)
    _elipse(cr, 71, 68, 5.5, 3.3)
    cr.fill()

    if expr in ("dormindo", "feito", "curtindo", "tonto"):
        olhar = (0.0, 0.0)
    ox, oy = olhar[0] * 3, olhar[1] * 2
    cr.set_source_rgb(*ESCURO)
    cr.set_line_width(4)

    def abertos(rx, ry):
        for x in (35, 61):
            _elipse(cr, x + ox, 56 + oy, rx, ry * abertura)
        cr.fill()
        if abertura > 0.5:
            cr.set_source_rgb(1, 1, 1)
            for x in (35, 61):
                _elipse(cr, x + ox + rx * 0.35, 56 + oy - ry * 0.4, rx * 0.32, rx * 0.32)
            cr.fill()
            cr.set_source_rgb(*ESCURO)

    if expr in ("feito", "curtindo"):
        _arco(cr, 35, 59, 6.5, 1.15, 1.85)
        _arco(cr, 61, 59, 6.5, 1.15, 1.85)
    elif expr == "dormindo":
        _arco(cr, 35, 55, 6, 0.15, 0.85)
        _arco(cr, 61, 55, 6, 0.15, 0.85)
    elif expr == "tonto":
        cr.set_line_width(2.4)
        for x in (35, 61):
            cr.new_path()
            a = 0.0
            while a < 4 * math.pi:
                cr.line_to(x + 0.6 * a * math.cos(a), 56 + 0.6 * a * math.sin(a))
                a += 0.3
            cr.stroke()
    elif expr == "atento":
        abertos(8, 10)
    elif expr == "surpreso":
        abertos(6.5, 6.5)
    else:
        abertos(7, 9)
    if expr in ("preocupado", "irritado"):
        cr.set_line_width(2.8)
        s = 1 if expr == "irritado" else -1
        _linha(cr, 28, 44 - 2 * s, 40, 44 + 2 * s)
        _linha(cr, 56, 44 + 2 * s, 68, 44 - 2 * s)

    cr.set_line_width(2.6)
    if expr in ("feito", "curtindo"):
        _arco(cr, 48, 66, 4.5, 0.2, 0.8)
    elif expr in ("surpreso", "atento"):
        _elipse(cr, 48, 70, 2.6, 3.4 if expr == "surpreso" else 2.2)
        cr.fill()
    elif expr == "preocupado":
        _arco(cr, 48, 74, 4, 1.25, 1.75)
    elif expr == "irritado":
        _linha(cr, 44, 71, 52, 71)
    elif expr == "tonto":
        cr.new_path()
        cr.move_to(42, 71)
        cr.curve_to(45, 68, 47, 74, 50, 71)
        cr.curve_to(52, 69, 53, 70, 54, 71)
        cr.stroke()
    elif expr == "dormindo":
        _elipse(cr, 48, 70, 1.6, 1.6)
        cr.fill()
    else:
        _arco(cr, 48, 67.5, 2.8, 0.25, 0.75)
    cr.restore()

    if expr == "curtindo":
        cr.set_source_rgb(*NOTA)
        cr.set_line_width(2.4)
        _linha(cr, 84, 6, 84, 18)
        _elipse(cr, 81.5, 19, 3, 2.6)
        cr.fill()
    elif expr == "dormindo":
        cr.set_source_rgb(*ZZ)
        cr.set_line_width(2.4)
        cr.new_path()
        cr.move_to(76, 8)
        cr.line_to(84, 8)
        cr.line_to(76, 17)
        cr.line_to(84, 17)
        cr.stroke()
    cr.restore()


class Animacao:
    """Piscar e respirar a partir do relógio de quadros (segundos); só avança quando o widget pede um quadro."""

    def __init__(self):
        self._piscar_em = None

    def estado(self, t: float, expr: str) -> tuple[float, float]:
        """(abertura, respiro) no instante t."""
        if self._piscar_em is None:
            self._piscar_em = t + 1.5 + random.random() * 2
        abertura = 1.0
        ms = (t - self._piscar_em) * 1000
        if ms >= 200:
            self._piscar_em = t + 2 + random.random() * 3
        elif ms >= 0:  # fecha em 70 ms, abre em 130 ms
            abertura = 1 - 0.94 * ms / 70 if ms < 70 else 0.06 + 0.94 * (ms - 70) / 130
        periodo, amplitude = (4.2, 0.035) if expr == "dormindo" else (3.2, 0.022)
        return abertura, math.sin(2 * math.pi * t / periodo) * amplitude


def expressao(device: dict | None) -> str:
    if not device or not device.get("connected"):
        return "dormindo"
    return "curtindo" if device.get("audio") else "conectado"
