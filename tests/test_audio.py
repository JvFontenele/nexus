"""Relógio do áudio que chega do dispositivo: descarta o que vem adiantado, recomeça depois de um atraso."""
import unittest

from nexus import audio


class TestJitter(unittest.TestCase):
    def test_rajada_e_atraso(self):
        now = [0.0]
        j = audio.Jitter(max_lead=0.03, clock=lambda: now[0])
        self.assertEqual([j.admit() for _ in range(6)], [True, True, True, True, False, False])  # rajada: só 30 ms à frente
        now[0] = 0.04
        self.assertTrue(j.admit())  # o tempo andou: cabe de novo
        now[0] = 1.0  # a rede parou: o pw-cat tocou silêncio
        self.assertEqual([j.admit() for _ in range(4)], [True, True, True, True])

    def test_props_sem_injecao(self):
        argv = audio.playback_argv('POCO "x" }{ y', "abc", target=7)
        self.assertNotIn('"x"', argv[3])
        self.assertEqual(argv[-3:], ["--target", "7", "-"])


if __name__ == "__main__":
    unittest.main()
