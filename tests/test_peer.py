"""PC↔PC: frames binários, receptor, e dois/três nexusd locais (pareamento, posse exclusiva, anti-laço,
mandado para casa, liberação garantida). O injetor do RemoteDesktop é falso: nada toca a área de trabalho."""
import asyncio
import os
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from nexus import inject, kvmwire, peer
from nexus.daemon import Daemon


class Clock:
    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset


class TestWire(unittest.TestCase):
    A2B = kvmwire.TO_TARGET

    def test_roundtrip(self):
        recs = [(kvmwire.ENTER, 0, 32768), (kvmwire.MOVE, -5, 7), (kvmwire.KEY, 30, 1), (kvmwire.BUTTON, 0x110, 0),
                (kvmwire.WHEEL, -2, 1), (kvmwire.LEAVE,), (kvmwire.PING,)]
        frame = kvmwire.encode(9, recs)
        n = kvmwire.frame_length(frame[:4])
        self.assertEqual(kvmwire.decode(frame[4:4 + n], self.A2B), (9, recs))

    def test_rejects_bad_frames(self):
        good = kvmwire.encode(1, [(kvmwire.MOVE, 1, 1)])[4:]
        bad = {
            "versão": bytes([2]) + good[1:],
            "zero registros": good[:5] + bytes([0]),
            "seq zero": struct.pack(">BIB", 1, 0, 1) + good[6:],
            "truncado": good[:-1],
            "sobrando": good + b"\x00",
            "tipo desconhecido": good[:6] + bytes([0x7F]),
            "direção errada": struct.pack(">BIB", 1, 1, 1) + bytes([kvmwire.HOME]) + struct.pack(">H", 1),
            "movimento grande": struct.pack(">BIB", 1, 1, 1) + bytes([kvmwire.MOVE]) + struct.pack(">hh", 2001, 0),
            "tecla 0": struct.pack(">BIB", 1, 1, 1) + bytes([kvmwire.KEY]) + struct.pack(">HB", 0, 1),
            "down 2": struct.pack(">BIB", 1, 1, 1) + bytes([kvmwire.KEY]) + struct.pack(">HB", 30, 2),
            "botão fora": struct.pack(">BIB", 1, 1, 1) + bytes([kvmwire.BUTTON]) + struct.pack(">HB", 0x118, 1),
            "roda grande": struct.pack(">BIB", 1, 1, 1) + bytes([kvmwire.WHEEL]) + struct.pack(">bb", 11, 0),
            "borda 4": struct.pack(">BIB", 1, 1, 1) + bytes([kvmwire.ENTER]) + struct.pack(">BH", 4, 0),
            "n maior que os registros": struct.pack(">BIB", 1, 1, 2) + good[6:],
        }
        for name, payload in bad.items():
            with self.subTest(name), self.assertRaises(kvmwire.WireError):
                kvmwire.decode(payload, self.A2B)
        with self.assertRaises(kvmwire.WireError):
            kvmwire.frame_length(struct.pack(">I", kvmwire.MAX_FRAME + 1))
        with self.assertRaises(kvmwire.WireError):
            kvmwire.encode(1, [(kvmwire.MOVE, 0, 0)] * (kvmwire.MAX_RECORDS + 1))

    def test_sequence_and_rate(self):
        clock = Clock()
        inb = kvmwire.Inbound(clock)
        inb.check(1, [(kvmwire.PING,)])
        for seq in (1, 3):  # duplicado, pulado
            with self.assertRaises(kvmwire.WireError):
                inb.check(seq, [(kvmwire.PING,)])
        inb = kvmwire.Inbound(clock)
        with self.assertRaises(kvmwire.WireError):  # movimento acumulado acima do limite em 1 s
            for seq in range(1, 100):
                inb.check(seq, [(kvmwire.MOVE, 2000, 2000)] * 8)
        inb = kvmwire.Inbound(clock)
        with self.assertRaises(kvmwire.WireError):  # frames por segundo
            for seq in range(1, kvmwire.FRAMES_PER_S + 2):
                inb.check(seq, [(kvmwire.PING,)])


class FakeInjector:
    def __init__(self, on_lost=lambda reason: None):
        self.on_lost = on_lost
        self.calls = []
        self.zones = [(0, 0, 1920, 1080)]
        self.closed = False

    def start(self):
        pass

    def move_abs(self, x, y):
        self.calls.append(("move", round(x), round(y)))
        return True

    def key(self, code, down):
        self.calls.append(("key", code, down))
        return True

    def button(self, code, down):
        self.calls.append(("button", code, down))
        return True

    def wheel(self, v, h):
        self.calls.append(("wheel", v, h))
        return True

    def close(self):
        self.closed = True


class TestReceiver(unittest.TestCase):
    def test_enter_move_home_and_release(self):
        inj, clock = FakeInjector(), Clock()
        r = inject.Receiver(inj, [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)], clock)
        with self.assertRaises(kvmwire.WireError):
            r.handle([(kvmwire.KEY, 30, 1)])  # sem foco
        r.handle([(kvmwire.ENTER, 0, 32768)])  # entra pela esquerda, no meio
        self.assertEqual(inj.calls[-1], ("move", 2, 540))
        r.handle([(kvmwire.KEY, 30, 1), (kvmwire.BUTTON, 0x110, 1), (kvmwire.MOVE, 500, 0)])
        self.assertEqual(r.keys, {30})
        out = r.handle([(kvmwire.MOVE, -520, 0)])  # chega à borda: 18 px passam
        self.assertEqual(out, [])
        out = r.handle([(kvmwire.MOVE, -30, 0)])
        self.assertEqual(out[0][0], kvmwire.HOME)
        self.assertAlmostEqual(out[0][1] / 65535, 540 / 1079, places=2)
        self.assertFalse(r.focus)
        self.assertIn(("key", 30, False), inj.calls)
        self.assertIn(("button", 0x110, False), inj.calls)  # soltou tudo ao voltar

    def test_inner_edge_is_not_home(self):
        inj = FakeInjector()
        r = inject.Receiver(inj, [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)], Clock())
        r.handle([(kvmwire.ENTER, 1, 0)])  # entra pela direita do monitor da direita
        self.assertEqual(inj.calls[-1][1], 3837)
        out = r.handle([(kvmwire.MOVE, -2000, 0), (kvmwire.MOVE, -2000, 0)])
        self.assertEqual(out, [])  # atravessa para o outro monitor e para na borda esquerda dele
        self.assertEqual(r.pos[0], 0)


class TestTokenStore(unittest.TestCase):
    def test_0600_and_forget(self):
        with tempfile.TemporaryDirectory() as d:
            t = inject.TokenStore(Path(d) / "rd.token")
            self.assertEqual(t.read(), "")
            t.write("segredo")
            self.assertEqual(stat.S_IMODE(os.stat(t.path).st_mode), 0o600)
            self.assertEqual(t.read(), "segredo")
            t.write("")
            self.assertFalse(t.path.exists())


class TestRemoteDesktop(unittest.TestCase):
    def test_token_flow_and_closed_session(self):
        from nexus import capture

        class Bus:
            unique_name = ":1.9"

            def __init__(self):
                self.calls, self.queue = [], []

            def call(self, iface, method, sig, args):
                self.calls.append((method, args))
                opts = next((a for a in args if isinstance(a, dict) and "handle_token" in a), None)
                if opts is None:
                    return ()
                token = opts["handle_token"].unpack()
                res = {"CreateSession": {"session_handle": "/s/1"}, "SelectDevices": {},
                       "Start": {"devices": 3, "restore_token": "novo"}}[method]
                self.queue.append((f"/org/freedesktop/portal/desktop/request/1_9/{token}", capture.REQUEST,
                                   "Response", (0, res)))
                return ()

            def call_fd(self, iface, method, sig, args):
                a, self.peer = socket.socketpair()
                return a.detach()

            def receive(self):
                out, self.queue = self.queue, []
                return out

            def close(self):
                pass

        class Sender:
            def __init__(self, fd):
                self.sock = socket.socket(fileno=fd)

            def fileno(self):
                return self.sock.fileno()

            def events(self):
                return []

            def regions(self):
                return [(0, 0, 1920, 1080)]

            def close(self):
                self.sock.close()

        with tempfile.TemporaryDirectory() as d:
            tokens = inject.TokenStore(Path(d) / "rd.token")
            tokens.write("antigo")
            bus, lost = Bus(), []
            rd = inject.RemoteDesktop(tokens, lost.append, bus_factory=lambda: bus, sender_factory=Sender)
            rd.start()
            select_opts = next(a for m, a in bus.calls if m == "SelectDevices")[1]
            self.assertEqual(select_opts["restore_token"].unpack(), "antigo")
            self.assertEqual(select_opts["persist_mode"].unpack(), 2)
            self.assertEqual(tokens.read(), "novo")
            self.assertEqual(rd.zones, [(0, 0, 1920, 1080)])
            bus.queue.append(("/s/1", inject.SESSION, "Closed", ({},)))  # o GNOME fechou a sessão por fora
            rd.move_abs(1, 1)
            for _ in range(100):
                if lost:
                    break
                time.sleep(0.02)
            self.assertTrue(lost)
            self.assertEqual(tokens.read(), "")  # não confia mais no token
            rd.close()


class TestTwoDaemons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loop = asyncio.new_event_loop()
        threading.Thread(target=cls.loop.run_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["NEXUS_STATE_DIR"] = str(Path(cls.tmp.name) / "state")

    @classmethod
    def tearDownClass(cls):
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.tmp.cleanup()

    def call(self, coro, timeout=15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def daemon(self, name):
        d = Daemon(Path(tempfile.mkdtemp(dir=self.tmp.name)), ["127.0.0.1"], 0, clock=Clock())
        d.name = name
        d._pairing_notice = lambda name: None  # nunca notificar a área de trabalho real
        d.injectors = []

        def factory(on_lost):
            inj = FakeInjector(on_lost)
            d.injectors.append(inj)
            return inj
        d.injector_factory = factory
        self.call(d.start())
        self.daemons.append(d)
        return d

    def setUp(self):
        self.daemons, self.sinks = [], []
        self.a, self.b = self.daemon("pc-a"), self.daemon("pc-b")

    def tearDown(self):
        for s in self.sinks:
            s.close()
        for d in self.daemons:
            self.call(d.close())

    def cmd(self, d, **req):
        return self.call(d._control_cmd(req))

    def pair(self, ctrl, target):
        started = self.cmd(ctrl, cmd="peer.pair", host="127.0.0.1", port=target.bound_port)
        self.assertTrue(started["ok"], started)
        for _ in range(100):
            pending = self.call(self._pending(target))
            if pending:
                break
            time.sleep(0.02)
        self.assertEqual(pending[0]["code"], started["code"])  # os dois PCs mostram o mesmo código
        self.assertEqual(pending[0]["kind"], "pc")
        self.assertTrue(self.call(target.confirm(pending[0]["pid"], True))["ok"])
        done = self.cmd(ctrl, cmd="peer.confirm", pid=started["pid"], accept=True)
        self.assertTrue(done["ok"], done)
        self.assertEqual(done["device_id"], target.fp_s)
        return ctrl.peers.records[target.fp_s]

    async def _pending(self, d):
        return d.list_pending()

    def sink(self, ctrl, rec, homes=None, lost=None):
        s = peer.PeerSink(rec, ctrl.cert, ctrl.key, on_home=(homes if homes is not None else []).append,
                          on_lost=(lost if lost is not None else []).append)
        self.sinks.append(s)
        return s

    def wait(self, cond, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def allow(self, target, ctrl):
        self.assertTrue(self.cmd(target, cmd="devices.allow", device_id=ctrl.fp_s, action="input.inject")["ok"])

    def test_pair_attach_inject_home_and_release(self):
        rec = self.pair(self.a, self.b)
        self.assertEqual((rec["host"], rec["port"], rec["kind"]), ("127.0.0.1", self.b.bound_port, "pc"))
        s = self.sink(self.a, rec)
        with self.assertRaisesRegex(RuntimeError, "not_allowed"):  # input.inject desligada por padrão
            s.start()
        self.allow(self.b, self.a)
        homes = []
        s = self.sink(self.a, rec, homes)
        s.start()
        self.assertEqual(self.cmd(self.b, cmd="input.status")["owner"], self.a.fp_s)
        inj = self.b.injectors[-1]
        s.enter("left", 0.5)
        s.key(30, True)
        s.button(0x111, True)
        s.move(300, 0)
        self.assertTrue(self.wait(lambda: ("move", 302, 540) in inj.calls))
        self.assertIn(("key", 30, True), inj.calls)
        s.move(-300, 0)
        s.move(-50, 0)  # empurra a borda esquerda do B
        self.assertTrue(self.wait(lambda: homes))
        self.assertAlmostEqual(homes[0], 540 / 1079, places=2)
        self.assertIn(("key", 30, False), inj.calls)       # o B soltou o que o A apertou
        self.assertIn(("button", 0x111, False), inj.calls)
        s.enter("left", 0.2)
        s.key(31, True)
        self.assertTrue(self.wait(lambda: ("key", 31, True) in inj.calls))
        s.close()  # desconexão: o B solta tudo e libera a posse
        self.assertTrue(self.wait(lambda: ("key", 31, False) in inj.calls))
        self.assertTrue(self.wait(lambda: self.cmd(self.b, cmd="input.status")["owner"] is None))
        self.assertTrue(inj.closed)

    def test_exclusive_ownership_under_concurrency(self):
        c = self.daemon("pc-c")
        ra, rc = self.pair(self.a, self.b), self.pair(c, self.b)
        self.allow(self.b, self.a)
        self.allow(self.b, c)
        sa, sc = self.sink(self.a, ra), self.sink(c, rc)
        results = {}

        def go(name, s):
            try:
                s.start()
                results[name] = "ok"
            except RuntimeError as e:
                results[name] = str(e)
        threads = [threading.Thread(target=go, args=x) for x in (("a", sa), ("c", sc))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sorted(results.values())[0], "ok")
        self.assertIn("owned", sorted(results.values())[1])  # nunca duas posses
        self.assertEqual(len([i for i in self.b.injectors if not i.closed]), 1)

    def test_anti_loop(self):
        rec = self.pair(self.a, self.b)
        self.allow(self.b, self.a)
        self.b.kvm = {"device_id": "x"}  # o B está controlando alguém: não pode ser controlado
        with self.assertRaisesRegex(RuntimeError, "busy"):
            self.sink(self.a, rec).start()
        self.b.kvm = None
        self.sink(self.a, rec).start()
        # o B controlado não abre a própria captura
        self.b.store.update(self.a.fp_s, "arrangement", {"edge": "right", "monitor": [0, 0, 10, 10], "start": 0, "end": 1})
        self.b.store.allow(self.a.fp_s, "input.control")
        self.assertIn("controlado", self.cmd(self.b, cmd="input.start", device_id=self.a.fp_s)["error"])

    def test_sent_home_cooldown_and_cancel(self):
        rec = self.pair(self.a, self.b)
        self.allow(self.b, self.a)
        lost = []
        s = self.sink(self.a, rec, lost=lost)
        s.start()
        s.enter("left", 0.5)
        s.key(30, True)
        self.assertTrue(self.wait(lambda: self.b.injectors and ("key", 30, True) in self.b.injectors[-1].calls))
        self.assertTrue(self.cmd(self.b, cmd="input.release")["ok"])  # o usuário do B manda para casa
        self.assertTrue(self.wait(lambda: lost))
        self.assertIn("para casa", lost[0])
        self.assertIn(("key", 30, False), self.b.injectors[-1].calls)
        with self.assertRaisesRegex(RuntimeError, "sent_home"):
            self.sink(self.a, rec).start()
        self.b.clock.offset += 6  # passou o prazo (timer monotônico)
        self.sink(self.a, rec).start()

    def test_cancel_sent_home_by_allow_and_permission_removal(self):
        rec = self.pair(self.a, self.b)
        self.allow(self.b, self.a)
        s = self.sink(self.a, rec)
        s.start()
        self.cmd(self.b, cmd="input.release")
        self.assertTrue(self.wait(lambda: self.cmd(self.b, cmd="input.status")["owner"] is None))
        self.allow(self.b, self.a)  # liberar de novo cancela o "mandado para casa"
        lost = []
        s = self.sink(self.a, rec, lost=lost)
        s.start()
        self.assertTrue(self.cmd(self.b, cmd="devices.deny", device_id=self.a.fp_s, action="input.inject")["ok"])
        self.assertTrue(self.wait(lambda: lost))
        self.assertIn("não permite", lost[0])

    def test_protocol_violation_releases(self):
        rec = self.pair(self.a, self.b)
        self.allow(self.b, self.a)
        s = self.sink(self.a, rec)
        s.start()
        s.enter("left", 0.5)
        s.key(30, True)
        inj = self.b.injectors[-1]
        self.assertTrue(self.wait(lambda: ("key", 30, True) in inj.calls))
        with s._lock:  # seq repetido: o B solta tudo e fecha
            s.sock.sendall(kvmwire.encode(s._seq, [(kvmwire.PING,)]))
        self.assertTrue(self.wait(lambda: ("key", 30, False) in inj.calls))
        self.assertTrue(self.wait(lambda: self.cmd(self.b, cmd="input.status")["owner"] is None))

    def test_timeout_releases(self):
        rec = self.pair(self.a, self.b)
        self.allow(self.b, self.a)
        s = self.sink(self.a, rec)
        s.start()
        s.enter("left", 0.5)
        s.key(30, True)
        inj = self.b.injectors[-1]
        s._closed = True  # para de mandar ping sem fechar o socket: o B espera INPUT_DEAD e solta tudo
        self.assertTrue(self.wait(lambda: ("key", 30, False) in inj.calls, timeout=6))


if __name__ == "__main__":
    unittest.main()
