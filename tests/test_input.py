"""Teclado e mouse compartilhados: libei (ctypes real), captura com portal falso, HID do scrcpy e travessia."""
import socket
import struct
import threading
import time
import unittest

from nexus import android_input as ai
from nexus import capture, eiread, kvm


class TestEiread(unittest.TestCase):
    def test_real_libei_sees_disconnect(self):
        a, b = socket.socketpair()
        ctx = eiread.Context(a.detach())
        self.assertGreaterEqual(ctx.fileno(), 0)
        b.close()
        self.assertIn(("disconnect",), ctx.events())
        ctx.close()
        ctx.close()  # idempotente


class TestBarriers(unittest.TestCase):
    def test_outer_edge_only(self):
        zones = [(0, 0, 1920, 1080), (1920, 0, 1280, 1024)]
        # à direita: a borda da tela 2 e o pedaço da tela 1 abaixo dela (1024–1080), que também é externo
        self.assertEqual(capture.barriers(zones, "right"), [(1920, 1024, 1920, 1079), (3200, 0, 3200, 1023)])
        self.assertEqual(capture.barriers(zones, "left"), [(0, 0, 0, 1079)])
        # só o trecho do arranjo: metade de baixo da borda direita do monitor da direita
        self.assertEqual(capture.barriers(zones, "right", ((1920, 0, 1280, 1024), 0.5, 1.0)), [(3200, 512, 3200, 1023)])
        # borda interna (direita do monitor da esquerda, encostada no outro): nada
        self.assertEqual(capture.barriers(zones, "right", ((0, 0, 1920, 1080), 0.0, 0.5)), [])
        with self.assertRaises(capture.PortalError):
            capture.barriers(zones, "right", ((5000, 0, 10, 10), 0.0, 1.0))


class FakeBus:
    unique_name = ":1.42"

    def __init__(self):
        self.calls = []
        self.queue = []
        self.closed = False
        self.handle = "/org/freedesktop/portal/desktop/session/1_42/s"

    def _respond(self, token, results):
        self.queue.append((f"/org/freedesktop/portal/desktop/request/1_42/{token}", capture.REQUEST, "Response",
                           (0, results)))

    def call(self, iface, method, sig, args):
        self.calls.append((method, args))
        opts = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
        token = opts["handle_token"].unpack() if "handle_token" in opts else None
        if method == "CreateSession":
            self._respond(token, {"session_handle": self.handle, "capabilities": 3})
        elif method == "GetZones":
            self._respond(token, {"zones": [(1920, 1080, 0, 0)], "zone_set": 7})
        elif method == "SetPointerBarriers":
            self._respond(token, {"failed_barriers": []})
        return ()

    def call_fd(self, iface, method, sig, args):
        self.calls.append((method, args))
        a, self.peer = socket.socketpair()
        return a.detach()

    def receive(self):
        out, self.queue = self.queue, []
        return out

    def close(self):
        self.closed = True


class FakeEi:
    def __init__(self, fd):
        self.sock = socket.socket(fileno=fd)
        self.pending = []

    def fileno(self):
        return self.sock.fileno()

    def events(self):
        self.sock.recv(64)
        out, self.pending = self.pending, []
        return out

    def close(self):
        self.sock.close()


class Recorder:
    def __init__(self):
        self.log = []
        self.done = threading.Event()

    def activated(self, point, zones):
        self.log.append(("activated", point, zones))

    def events(self, evs):
        self.log.append(("events", evs))

    def deactivated(self):
        self.log.append(("deactivated",))

    def tick(self):
        pass

    def ended(self, reason):
        self.log.append(("ended", reason))
        self.done.set()


def wait_for(cond, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class TestCapture(unittest.TestCase):
    def test_session_flow(self):
        bus, eis, rec = FakeBus(), [], Recorder()
        cap = capture.Capture("right", rec, bus_factory=lambda: bus, ei_factory=lambda fd: eis.append(FakeEi(fd)) or eis[-1])
        cap.start()
        self.assertTrue(wait_for(lambda: any(m == "Enable" for m, _ in bus.calls)))
        barrier = next(a for m, a in bus.calls if m == "SetPointerBarriers")
        self.assertEqual(barrier[2][0]["position"].unpack(), (1920, 0, 1920, 1079))
        self.assertEqual(barrier[3], 7)
        # o ponteiro bate na barreira
        bus.queue.append(("/org/freedesktop/portal/desktop", capture.INTERFACE, "Activated",
                          (bus.handle, {"activation_id": 5, "cursor_position": (1919.0, 500.0), "barrier_id": 1})))
        eis[0].pending = [("motion", 1, 3.0, 0.0), ("frame", 1, 0)]
        bus.peer.send(b"x")
        self.assertTrue(wait_for(lambda: any(e[0] == "events" for e in rec.log)))
        self.assertEqual(rec.log[0], ("activated", (1919.0, 500.0), [(0, 0, 1920, 1080)]))
        self.assertEqual(next(e for e in rec.log if e[0] == "events")[1], [("motion", 1, 3.0, 0.0)])  # frame filtrado
        # sinal de outra sessão é ignorado
        bus.queue.append(("/", capture.INTERFACE, "Deactivated", ("/outra", {})))
        cap.release((1900, 500))
        self.assertTrue(wait_for(lambda: any(m == "Release" for m, _ in bus.calls)))
        opts = next(a for m, a in bus.calls if m == "Release")[1]
        self.assertEqual(opts["activation_id"].unpack(), 5)
        self.assertEqual(opts["cursor_position"].unpack(), (1900.0, 500.0))
        self.assertNotIn(("deactivated",), rec.log)
        cap.stop()
        self.assertTrue(rec.done.wait(3))
        self.assertEqual(rec.log[-1], ("ended", None))
        self.assertTrue(bus.closed)  # fechar o D-Bus é a volta garantida

    def test_refused_ends_with_reason(self):
        class RefusingBus(FakeBus):
            def call(self, iface, method, sig, args):
                token = args[1]["handle_token"].unpack()
                self.queue.append((f"/org/freedesktop/portal/desktop/request/1_42/{token}", capture.REQUEST,
                                   "Response", (1, {})))
                return ()
        rec = Recorder()
        capture.Capture("left", rec, bus_factory=RefusingBus).start()
        self.assertTrue(rec.done.wait(3))
        self.assertIn("não liberou", rec.log[-1][1])

    def test_bad_edge(self):
        with self.assertRaises(ValueError):
            capture.Capture("diagonal", Recorder())


class TestHid(unittest.TestCase):
    def test_keyboard_reports(self):
        h = ai.HidState()
        (msg,) = h.key(42, True)  # Shift esquerdo
        self.assertEqual(msg, struct.pack(">BHH", ai.UHID_INPUT, ai.KEYBOARD_ID, 8) + bytes([0x02, 0, 0, 0, 0, 0, 0, 0]))
        (msg,) = h.key(30, True)  # A
        self.assertEqual(msg[-8:], bytes([0x02, 0, 0x04, 0, 0, 0, 0, 0]))
        self.assertEqual(h.key(89, True)[0][-8:], bytes([0x02, 0, 0x04, 0x87, 0, 0, 0, 0]))  # "/?" do ABNT2
        self.assertEqual(h.key(999, True), [])  # tecla desconhecida não manda nada
        out = h.release_all()
        self.assertEqual(out[0][-8:], bytes(8))
        self.assertEqual(out[1][-7:], bytes(7))

    def test_media_keys_and_buttons(self):
        h = ai.HidState()
        self.assertEqual(h.key(164, True), [struct.pack(">BBiii", 0, 0, 85, 0, 0)])  # play/pause
        self.assertEqual(h.key(164, True), [])  # repetido
        self.assertEqual(h.release_all()[0], struct.pack(">BBiii", 0, 1, 85, 0, 0))  # mídia apertada é solta
        self.assertEqual(h.button(0x111, True)[0][-7], 0x02)  # botão direito
        self.assertEqual(h.mouse_report(dx=70000, dy=-5, wheel=-300), struct.pack("<Bhhbb", 2, 32767, -5, -127, 0))

    def test_create_and_clipboard_messages(self):
        msg = ai.uhid_create(1, "Nexus teclado", ai.KEYBOARD_DESC)
        self.assertEqual(msg[:8], struct.pack(">BHHHB", 12, 1, 0, 0, 13))
        self.assertEqual(struct.unpack(">H", msg[21:23])[0], len(ai.KEYBOARD_DESC))
        self.assertEqual(ai.set_clipboard(3, "é"), struct.pack(">BqBi", 9, 3, 0, 2) + "é".encode())

    def test_device_messages(self):
        data = (bytes([2]) + struct.pack(">HH", 1, 1) + b"\x02"
                + bytes([1]) + struct.pack(">q", 9)
                + bytes([0]) + struct.pack(">I", 3) + b"oi!")
        got, buf = [], bytearray(data)

        def recv(n):
            if len(buf) < n:
                raise ConnectionError
            out = bytes(buf[:n])
            del buf[:n]
            return out
        with self.assertRaises(ConnectionError):
            ai.read_device_messages(recv, got.append)
        self.assertEqual(got, ["oi!"])
        huge = bytearray(bytes([0]) + struct.pack(">I", ai.MAX_CLIPBOARD + 1))
        with self.assertRaises(ValueError):
            ai.read_device_messages(lambda n: bytes([huge.pop(0) for _ in range(n)]), got.append)

    def test_parse_accel(self):
        dump = "  Pointer Acceleration: 3.000\n    PointerVelocityControlParameters: scale=1.000, lowThreshold=500.000, highThreshold=3000.000, acceleration=3.000\n"
        self.assertEqual(ai.parse_accel(dump), (1.0, 500.0, 3000.0, 3.0))
        self.assertIsNone(ai.parse_accel("nada"))
        self.assertIsNone(ai.parse_accel(dump.replace("lowThreshold=500", "lowThreshold=9000")))

    def test_autoclick_read_only(self):
        from unittest import mock
        dev = ai.AndroidInput("7db4da6e")
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="1\n")) as run:
            self.assertTrue(dev.autoclick_on())
        self.assertEqual(run.call_args[0][0], ["adb", "-s", "7db4da6e", "shell", "settings", "get", "secure",
                                               "accessibility_autoclick_enabled"])  # só "get", nunca "put"
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="0\n")):
            self.assertFalse(dev.autoclick_on())

    def test_serial_validated(self):
        with self.assertRaises(ValueError):
            ai.AndroidInput("x; reboot")


class FakeSink:
    size = (1000, 2000)
    accel = (1.0, 500.0, 3000.0, 1.0)  # sem aceleração: os testes da travessia contam pixels diretos
    exact = False

    def __init__(self):
        self.log = []

    def __getattr__(self, name):
        return lambda *a: self.log.append((name, *a))

    def get_clipboard(self):
        return "do celular"


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


class TestTravessia(unittest.TestCase):
    def setUp(self):
        self.clock, self.sink, self.released, self.changes = Clock(), FakeSink(), [], []
        self.pc_clip = []
        self.t = kvm.Travessia("right", self.sink, self.released.append, clock=self.clock,
                               pc_clipboard=lambda: "do PC", set_pc_clipboard=self.pc_clip.append,
                               on_change=self._changed, on_end=lambda r: self.reasons.append(("fim", r)),
                               sleep=self._sleep)
        self.reasons = []
        self.zones = [(0, 0, 1920, 1080)]

    def _sleep(self, s):
        self.clock.t += s

    def _changed(self, away, reason=None):
        if away or not reason or not reason.startswith("solto"):
            self.changes.append(away)
        self.reasons.append(reason)

    def cross(self):
        self.t.activated((1919.0, 540.0), self.zones)
        self.t.events([("motion", 1, 30.0, 0.0)])
        self.assertFalse(self.t.away)  # 30 px ainda não vencem a resistência
        self.t.events([("motion", 1, 40.0, 0.0)])

    def test_resistance_then_cross(self):
        self.cross()
        self.assertTrue(self.t.away)
        self.assertEqual(self.released, [])
        self.assertIn(("set_clipboard", "do PC"), self.sink.log)
        self.assertEqual(self.changes, [True])
        # entra pela borda esquerda do celular, na mesma altura relativa
        self.assertEqual(self.t._pos[0], kvm.ENTRY_PX)  # um pouco para dentro da borda do celular
        self.assertAlmostEqual(self.t._pos[1], 540 / 1079 * 1999, delta=1)

    def test_slow_push_does_not_cross(self):
        self.t.activated((1919.0, 540.0), self.zones)
        for dx in (30.0, 5.0, 5.0, 5.0):  # a cada 0,95 s: nunca 40 px dentro de 1 s
            self.t.events([("motion", 1, dx, 0.0)])
            self.clock.t += 0.95
            self.t.tick()
        self.assertFalse(self.t.away)
        self.assertEqual(self.released, [])

    def test_let_go_when_coming_back_in_or_idle(self):
        self.t.activated((1919.0, 540.0), self.zones)
        self.t.events([("motion", 1, 10.0, 0.0), ("motion", 1, -5.0, 3.0)])
        self.assertFalse(self.t.away)
        self.assertEqual(len(self.released), 1)
        self.t.activated((1919.0, 100.0), self.zones)
        self.clock.t += 0.5
        self.t.tick()
        self.assertEqual(len(self.released), 1)  # meio segundo parado ainda não solta
        self.clock.t += 1
        self.t.tick()
        self.assertEqual(len(self.released), 2)
        self.assertEqual(self.reasons[:2], ["solto: voltou para dentro", "solto: parado na borda"])
        self.t.activated((1919.0, 100.0), self.zones)
        self.t.events([("key", 1, 30, True)])  # tecla durante o empurrão: devolve
        self.assertEqual(len(self.released), 3)
        self.assertNotIn("key", [e[0] for e in self.sink.log])

    def test_forward_and_double_tap_home(self):
        self.cross()
        self.sink.log.clear()
        self.t.events([("key", 1, 30, True), ("key", 1, 30, False), ("button", 1, 0x110, True),
                       ("scroll_discrete", 1, 0, 240), ("scroll", 1, 0, 20.0)])
        self.assertEqual(self.sink.log, [("key", 30, True), ("key", 30, False), ("button", 0x110, True),
                                         ("wheel", -2, 0)])  # scroll contínuo ignorado com o discreto junto
        self.t.events([("key", 1, 97, True), ("key", 1, 97, False)])
        self.clock.t += 0.2
        self.t.events([("key", 1, 97, True)])
        self.assertFalse(self.t.away)
        self.assertIn(("release_all",), self.sink.log)
        x, y = self.released[-1]
        self.assertEqual(x, 1919 - kvm.INSET_PX)
        self.assertEqual(self.pc_clip, ["do celular"])
        self.assertEqual(self.changes, [True, False])

    def test_slow_double_tap_or_key_between_does_not_go_home(self):
        self.cross()
        self.t.events([("key", 1, 97, True)])
        self.clock.t += 1
        self.t.events([("key", 1, 97, True)])
        self.t.events([("key", 1, 30, True)])
        self.t.events([("key", 1, 97, True)])
        self.assertTrue(self.t.away)

    def test_push_back_through_phone_edge(self):
        self.cross()
        self.t.events([("motion", 1, 300.0, -100.0)])  # anda para dentro do celular
        self.t.events([("motion", 1, -300.0, 0.0)])     # volta até a borda esquerda
        self.assertTrue(self.t.away)
        self.t.events([("motion", 1, -30.0, 0.0), ("motion", 1, -40.0, 0.0)])
        self.assertTrue(self.t.away)  # só 30 px passaram da borda
        self.t.events([("motion", 1, -40.0, 0.0)])
        self.assertFalse(self.t.away)
        self.assertEqual(self.reasons[-1], "empurrou a borda do celular")
        self.assertIn(("release_all",), self.sink.log)
        x, y = self.released[-1]
        self.assertEqual(x, 1919 - kvm.INSET_PX)
        self.assertAlmostEqual(y, (self.t._pos[1] / 1999) * 1079, delta=1)

    def test_sink_lost_and_deactivated(self):
        self.cross()
        self.t.sink_lost()
        self.assertFalse(self.t.away)
        self.assertEqual(self.pc_clip, [])  # sem servidor, não busca clipboard
        self.assertEqual(len(self.released), 1)
        self.cross()
        self.t.deactivated()  # o compositor encerrou: solta tudo, sem Release
        self.assertFalse(self.t.away)
        self.assertEqual(len(self.released), 1)
        self.assertEqual(self.sink.log.count(("release_all",)), 2)


class TestAceleracao(unittest.TestCase):
    def test_gain_follows_speed(self):
        clock = Clock()
        a = kvm.Aceleracao((1.0, 500.0, 3000.0, 3.0), clock)
        self.assertEqual(a.gain(5, 0), 1.0)  # primeira amostra: sem velocidade
        clock.t += 0.02
        self.assertEqual(a.gain(5, 0), 1.0)  # 250 contagens/s: abaixo de low
        clock.t += 0.02
        self.assertAlmostEqual(a.gain(35, 0), 1 + (1000 - 500) / 2500 * 2)  # 40 em 40 ms = 1000/s
        clock.t += 1
        self.assertEqual(a.gain(60, 0), 1.0)  # pausa maior que a janela: recomeça
        clock.t += 0.004
        self.assertEqual(a.gain(60, 0), 3.0)  # 15000/s: bem acima de high

    def test_entry_placement_with_android_gain(self):
        sink, clock = FakeSink(), Clock()
        sink.accel = (1.0, 500.0, 3000.0, 3.0)
        t = kvm.Travessia("right", sink, lambda p: None, clock=clock, sleep=lambda s: setattr(clock, "t", clock.t + s),
                          start=0.5, end=1.0)
        t.activated((1919.0, 700.0), [(0, 0, 1920, 1080)])  # trecho [540, 1080]: fração ≈ 0,3
        t.events([("motion", 1, 50.0, 0.0)])
        self.assertTrue(t.away)
        self.assertAlmostEqual(t._pos[1], (700 - 540) / 539 * 1999, delta=3)
        moves = [e[1:] for e in sink.log if e[0] == "move"]
        self.assertEqual(moves[0], (-2000, -4000))  # encosta no canto de cima à esquerda
        self.assertEqual(moves[1], (1, 1))          # passo de ganho 1
        rest = moves[2:]
        self.assertTrue(all(abs(dx) <= kvm.STEP_COUNTS and abs(dy) <= kvm.STEP_COUNTS for dx, dy in rest))
        # 1 + 3 × o resto chega ao ponto de entrada
        self.assertAlmostEqual(1 + 3 * sum(dy for _dx, dy in rest), t._pos[1], delta=3)
        self.assertAlmostEqual(1 + 3 * sum(dx for dx, _dy in rest), kvm.ENTRY_PX, delta=3)


class TestTrecho(unittest.TestCase):
    def test_trecho(self):
        self.assertEqual(kvm.trecho((1920, 0, 1920, 1080), "right", 0.25, 0.75), (1920, 270, 1920, 540))
        self.assertEqual(kvm.trecho((0, 0, 1920, 1080), "top", 0.0, 0.5), (0, 0, 960, 1080))


class TestArranjo(unittest.TestCase):
    MONS = [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]

    def test_snap_to_outer_right_edge(self):
        from nexus import arranjo
        size = arranjo.phone_size(self.MONS)
        arr, rect = arranjo.snap(self.MONS, (3900, 540), size)
        self.assertEqual(arr["edge"], "right")
        self.assertEqual(arr["monitor"], [1920, 0, 1920, 1080])
        self.assertAlmostEqual(arr["end"] - arr["start"], 0.45, places=2)  # o celular cobre 45% da borda
        self.assertAlmostEqual((arr["start"] + arr["end"]) / 2, 0.5, places=2)
        self.assertEqual(rect[0], 3840)
        self.assertEqual(arranjo.rect_of(arr, self.MONS, size)[:2], rect[:2])

    def test_never_snaps_to_inner_edge_and_clamps(self):
        from nexus import arranjo
        size = arranjo.phone_size(self.MONS)
        arr, _ = arranjo.snap(self.MONS, (1920, 540), size)  # entre os dois monitores: não há borda livre ali
        self.assertNotEqual((arr["edge"], arr["monitor"][0]), ("right", 0))
        self.assertNotEqual((arr["edge"], arr["monitor"][0]), ("left", 1920))
        arr, _ = arranjo.snap(self.MONS, (3900, 5000), size)  # muito abaixo: preso no fim da borda
        self.assertTrue(arr["end"] <= 1.0 and arr["start"] >= 0.0)
        self.assertIsNone(arranjo.rect_of({**arr, "monitor": [9, 9, 9, 9]}, self.MONS, size))


if __name__ == "__main__":
    unittest.main()
