"""O Nexo desenha todas as expressões e a animação pisca e respira dentro dos limites."""
import unittest

import cairo

from nexus import mascote


class TestMascote(unittest.TestCase):
    def test_desenha_todas_as_expressoes(self):
        sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, 96, 88)
        for expr in mascote.EXPRESSOES:
            cr = cairo.Context(sup)
            mascote.desenhar(cr, 96, 88, expr, (1, -1), 0.5, 0.03)
        self.assertGreater(sup.get_data()[4 * (60 * 96 + 48) + 3], 0)  # o corpo cobre o centro

    def test_pisca_e_respira(self):
        a = mascote.Animacao()
        a.estado(0.0, "conectado")
        inicio = a._piscar_em
        self.assertEqual(a.estado(inicio - 0.01, "conectado")[0], 1.0)
        self.assertLess(a.estado(inicio + 0.07, "conectado")[0], 0.1)
        self.assertGreater(a.estado(inicio + 0.19, "conectado")[0], 0.9)
        a.estado(inicio + 0.25, "conectado")
        self.assertGreater(a._piscar_em, inicio + 2)  # agenda a próxima piscada
        self.assertTrue(all(abs(a.estado(t / 10, "dormindo")[1]) <= 0.035 for t in range(100)))


if __name__ == "__main__":
    unittest.main()
