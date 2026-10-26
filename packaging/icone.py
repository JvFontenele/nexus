"""Gera o ícone do Nexus a partir do Nexo (nexus/mascote.py): SVG do app do PC e o primeiro plano do ícone
adaptativo do Android. Rode de novo se o mascote mudar: python3 packaging/icone.py"""
import math
import sys
from pathlib import Path

import cairo

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from nexus import mascote  # noqa: E402

FUNDO = (0.086, 0.094, 0.114)  # #16181D, o fundo dos cartões dos apps


def quadrado(cr, x, y, lado, raio):
    cr.new_sub_path()
    for cx, cy, a in ((x + lado - raio, y + raio, -math.pi / 2), (x + lado - raio, y + lado - raio, 0),
                      (x + raio, y + lado - raio, math.pi / 2), (x + raio, y + raio, math.pi)):
        cr.arc(cx, cy, raio, a, a + math.pi / 2)
    cr.close_path()


def nexo(cr, centro, lado):
    cr.save()
    cr.translate(centro - lado / 2, centro - lado / 2)
    mascote.desenhar(cr, lado, lado, "conectado")
    cr.restore()


def svg_pc(caminho: Path):
    with cairo.SVGSurface(str(caminho), 512, 512) as s:
        cr = cairo.Context(s)
        quadrado(cr, 32, 32, 448, 100)  # margem e cantos no estilo dos ícones do GNOME
        cr.set_source_rgb(*FUNDO)
        cr.fill()
        nexo(cr, 256, 340)


def android(res: Path):
    lado = 432  # 108 dp em xxxhdpi; o que importa fica nos 66 dp do meio (264 px)
    pasta = res / "mipmap-xxxhdpi"
    pasta.mkdir(parents=True, exist_ok=True)
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, lado, lado)
    nexo(cairo.Context(s), lado / 2, 250)
    s.write_to_png(str(pasta / "ic_launcher_foreground.png"))


if __name__ == "__main__":
    svg_pc(RAIZ / "packaging" / "dev.nexus.App.svg")
    android(RAIZ / "android" / "app" / "src" / "main" / "res")
