"""Teclado e mouse Bluetooth LE (HOGP), sem Bluetooth real: Report Map, tabela evdev→HID e relatórios."""
import unittest

from nexus import android_input as ai
from nexus import bthid


class FakeBtHid(bthid.BtHid):
    def __init__(self):
        super().__init__(confirm=lambda name, code: False)
        self.sent = []
        self.later = []

    def _later(self, fn):
        self.later.append(fn)

    def _send(self, which, report):
        self.sent.append((which, report))


class TestReportMap(unittest.TestCase):
    def test_bytes(self):
        m = bthid.REPORT_MAP
        self.assertEqual(m[:8], bytes([0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01]))  # teclado, id 1
        k = len(ai.KEYBOARD_DESC) + 2
        self.assertEqual(m[k:k + 8], bytes([0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x02]))  # mouse, id 2
        self.assertEqual(len(m), len(ai.KEYBOARD_DESC) + len(ai.MOUSE_DESC) + 4 + len(bthid.CONSUMER_DESC))
        self.assertEqual(m.count(b"\x85"), 3)
        c = len(ai.KEYBOARD_DESC) + len(ai.MOUSE_DESC) + 4
        self.assertEqual(m[c:c + 8], bytes([0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x03]))  # Consumer, id 3
        self.assertEqual(m[-1:], b"\xc0")


class TestTabela(unittest.TestCase):
    def test_abnt2(self):
        self.assertEqual(ai.KEYS[89], 0x87)   # KEY_RO: "/?" do ABNT2
        self.assertEqual(ai.KEYS[86], 0x64)   # KEY_102ND: "\|" do ABNT2
        self.assertEqual(ai.KEYS[39], 0x33)   # ç
        self.assertEqual(ai.KEYS[40], 0x34)   # ~ ^ (tecla morta)
        self.assertEqual(ai.KEYS[30], 0x04)   # a


class TestRelatorios(unittest.TestCase):
    def test_teclas_e_modificador(self):
        h = FakeBtHid()
        h.key(42, True)   # Shift esquerdo
        h.key(89, True)   # "/?" → "?"
        self.assertEqual(h.sent[-1], ("keyboard", bytes([0x02, 0, 0x87, 0, 0, 0, 0, 0])))
        h.key(89, False)
        h.key(42, False)
        self.assertEqual(h.sent[-1], ("keyboard", bytes(8)))

    def test_ignora_desconhecida(self):
        h = FakeBtHid()
        h.key(0x2FF, True)
        h.button(0x150, True)
        self.assertEqual(h.sent, [])

    def test_consumer_control(self):
        self.assertEqual(set(bthid.CONSUMER), set(ai.MEDIA))  # as mesmas teclas de mídia do caminho adb
        h = FakeBtHid()
        h.key(115, True)   # volume +
        self.assertEqual(h.sent[-1], ("consumer", bytes([0xE9, 0x00])))
        h.key(115, False)
        self.assertEqual(h.sent[-1], ("consumer", bytes(2)))
        h.key(164, True)   # play/pause preso
        h.sent.clear()
        h.release_all()
        self.assertIn(("consumer", bytes(2)), h.sent)  # soltar tudo solta a mídia também
        h.key(164, False)  # soltar de novo não manda nada
        self.assertNotIn(("consumer", bytes(2)), h.sent[3:])

    def test_close_only_stops_sending(self):
        h = FakeBtHid()
        h.key(30, True)
        h.close()
        self.assertEqual(h.sent[-2:], [("keyboard", bytes(8)), ("mouse", bytes(7))])  # solta tudo antes de parar

    def test_real_send_respects_close(self):
        sent = []

        class P:
            def send(self, which, report):
                sent.append(which)
        h = bthid.BtHid(P())
        h.key(30, True)
        h.close()
        n = len(sent)
        h.key(31, True)
        self.assertEqual(len(sent), n)

    def test_mouse(self):
        h = FakeBtHid()
        h.button(0x110, True)
        self.assertEqual(h.sent[-1], ("mouse", bytes([1, 0, 0, 0, 0, 0, 0])))
        h.move(-1, 100)
        h.move(-1, 200)  # juntados num relatório só, quando o temporizador vence
        self.assertEqual(len(h.later), 1)
        self.assertEqual(h.sent[-1], ("mouse", bytes([1, 0, 0, 0, 0, 0, 0])))
        h.later.pop()()
        self.assertEqual(h.sent[-1], ("mouse", bytes([1, 0xFE, 0xFF, 0x2C, 0x01, 0, 0])))
        h.move(5, 0)
        h.button(0x110, False)  # o clique sai depois do movimento pendente
        self.assertEqual(h.sent[-2:], [("mouse", bytes([1, 5, 0, 0, 0, 0, 0])), ("mouse", bytes(7))])
        h.button(0x110, True)
        h.wheel(1, -1)
        self.assertEqual(h.sent[-1], ("mouse", bytes([1, 0, 0, 0, 0, 1, 0xFF])))

    def test_release_all(self):
        h = FakeBtHid()
        h.key(29, True)
        h.key(30, True)
        h.button(0x111, True)
        h.sent.clear()
        h.release_all()
        self.assertEqual(h.sent, [("keyboard", bytes(8)), ("mouse", bytes(7))])
        h.key(30, True)  # nada ficou preso no estado
        self.assertEqual(h.sent[-1], ("keyboard", bytes([0, 0, 0x04, 0, 0, 0, 0, 0])))


if __name__ == "__main__":
    unittest.main()
