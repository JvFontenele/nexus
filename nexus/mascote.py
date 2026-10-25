"""O Nexo (mascote) em Cairo, nas mesmas coordenadas do design e do notch do Balão (viewBox 96×76)."""
import math

BRANCO = (0.953, 0.957, 0.969)
ESCURO = (0.078, 0.086, 0.106)
CINZA = (0.788, 0.8, 0.831)
AZUL = (0.557, 0.659, 1.0)
EXPRESSOES = ("conectado", "feito", "atento", "curtindo", "surpreso", "dormindo")


def _elipse(cr, cx, cy, rx, ry):
    cr.save()
    cr.translate(cx, cy)
    cr.scale(rx, ry)
    cr.new_sub_path()
    cr.arc(0, 0, 1, 0, 2 * math.pi)
    cr.restore()


def _arco(cr, cx, cy, r, de, ate):
    cr.new_path()
    cr.arc(cx, cy, r, de * math.pi, ate * math.pi)
    cr.stroke()


def desenhar(cr, largura: float, altura: float, expr: str):
    cr.save()
    escala = min(largura / 96, altura / 76)  # mantém a proporção dentro da área
    cr.translate((largura - 96 * escala) / 2, (altura - 76 * escala) / 2)
    cr.scale(escala, escala)
    x, y, w, h, r = 4, 6, 88, 64, 26
    cr.new_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()
    cr.set_source_rgb(*(CINZA if expr == "dormindo" else BRANCO))
    cr.fill()
    cr.set_source_rgb(*ESCURO)
    cr.set_line_width(5)
    cr.set_line_cap(1)  # ROUND
    if expr == "feito":
        _arco(cr, 36, 45, 7, 1.15, 1.85)
        _arco(cr, 60, 45, 7, 1.15, 1.85)
    elif expr == "curtindo":
        _arco(cr, 36, 33, 7, 0.15, 0.85)
        _arco(cr, 60, 33, 7, 0.15, 0.85)
        cr.set_source_rgb(*AZUL)
        cr.set_line_width(3)
        cr.new_path()
        cr.move_to(78, 4)
        cr.line_to(78, 16)
        cr.stroke()
        _elipse(cr, 75, 17, 4, 4)
        cr.fill()
    elif expr == "atento":
        _elipse(cr, 36, 38, 9, 9)
        _elipse(cr, 60, 38, 9, 9)
        cr.fill()
        cr.set_source_rgb(*BRANCO)
        _elipse(cr, 39, 35, 3, 3)
        _elipse(cr, 63, 35, 3, 3)
        cr.fill()
    elif expr == "surpreso":
        _elipse(cr, 36, 36, 7, 7)
        _elipse(cr, 60, 36, 7, 7)
        _elipse(cr, 48, 54, 5, 6)
        cr.fill()
    elif expr == "dormindo":
        cr.new_path()
        cr.move_to(30, 40)
        cr.line_to(42, 40)
        cr.move_to(54, 40)
        cr.line_to(66, 40)
        cr.stroke()
        cr.set_source_rgb(0.557, 0.573, 0.616)
        cr.set_line_width(2.4)
        cr.new_path()
        cr.move_to(74, 8)
        cr.line_to(82, 8)
        cr.line_to(74, 17)
        cr.line_to(82, 17)
        cr.stroke()
    else:  # conectado
        _elipse(cr, 36, 38, 5.5, 8)
        _elipse(cr, 60, 38, 5.5, 8)
        cr.fill()
    cr.restore()


def expressao(device: dict | None) -> str:
    if not device or not device.get("connected"):
        return "dormindo"
    return "curtindo" if device.get("audio") else "conectado"
