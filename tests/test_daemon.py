import asyncio
import os
import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from nexus import catalog
from nexus.actions import load_all
from nexus.daemon import Daemon, control
from nexus.proto.framing import encode, read_frame, recv_exact_from
from nexus.proto.identity import device_id, ensure_identity
from nexus.proto.messages import make
from nexus.proto.pairing import EXPIRES_IN, commit, new_nonce, sas_code
from nexus.proto.trust import client_context, pin_ok


class Clock:
    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset


class Client:
    def __init__(self, port, ident=None, present=True):
        self.ident = ident
        ctx = client_context(*ident) if (ident and present) else client_context()
        self.s = ctx.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5))
        self._recv = recv_exact_from(self.s)

    def send(self, type_, body):
        m = make(type_, body)
        self.s.sendall(encode(m))
        return m

    def raw(self, data: bytes):
        self.s.sendall(data)

    def recv(self):
        return read_frame(self._recv)

    def hello(self, with_cert=True):
        cert = self.ident[0]
        body = {"device_id": device_id(cert), "name": "teste", "kind": "phone", "app_version": "0", "protocol": [1]}
        if with_cert:
            body["cert_pem"] = cert.read_text()
        self.send("hello", body)
        return self.recv()

    def closed(self) -> bool:
        try:
            return self.s.recv(1) == b""
        except (ssl.SSLError, OSError):
            return True

    def close(self):
        self.s.close()


class TestDaemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_all()
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        os.environ["NEXUS_STATE_DIR"] = str(base / "state")
        cls.phone = ensure_identity(base / "phone")
        cls.tablet = ensure_identity(base / "tablet")
        cls.evil = ensure_identity(base / "evil")
        cls.fp_phone = device_id(cls.phone[0])
        if "test.say" not in catalog.REGISTRY:
            catalog.register(catalog.Action("test.say", "eco remoto", lambda p: p["text"],
                                            (catalog.Param("text"),), phone_safe=True))
            catalog.register(catalog.Action("test.file", "caminho", lambda p: str(p["p"]),
                                            (catalog.Param("p", "path"),), phone_safe=True))
        cls.loop = asyncio.new_event_loop()
        threading.Thread(target=cls.loop.run_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for k in ("test.say", "test.file"):
            catalog.REGISTRY.pop(k, None)
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.tmp.cleanup()

    def setUp(self):
        self.clock = Clock()
        self.data = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.d = Daemon(self.data, ["127.0.0.1"], 0, clock=self.clock)
        self.notices = []
        self.d._pairing_notice = self.notices.append  # nunca notificar a área de trabalho real nos testes
        self.call(self.d.start())
        self.port = self.d.bound_port
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.call(self.d.close())

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def on_loop(self, fn):
        async def run():
            return fn()
        return self.call(run())

    def client(self, ident=None, present=True):
        c = Client(self.port, ident, present)
        self.clients.append(c)
        return c

    def wait_pending(self):
        for _ in range(100):
            p = self.on_loop(self.d.list_pending)
            if p:
                return p[0]
            time.sleep(0.02)
        self.fail("sem pareamento pendente")

    def start_pairing(self, ident):
        """Cliente honesto até o reveal; devolve (cliente, sessão, código calculado no cliente)."""
        c = self.client(ident, present=False)
        srv = c.hello()
        self.assertEqual(srv["type"], "hello")
        self.assertTrue(pin_ok(c.s, srv["body"]["device_id"]))
        fp_c, fp_s = device_id(ident[0]), srv["body"]["device_id"]
        n_c = new_nonce()
        c.send("pair.request", {"commit": commit(n_c, fp_c, fp_s)})
        ch = c.recv()
        self.assertEqual(ch["type"], "pair.challenge")
        c.send("pair.reveal", {"session": ch["body"]["session"], "n_c": n_c})
        return c, ch["body"]["session"], sas_code(ch["body"]["session"], fp_s, fp_c, ch["body"]["n_s"], n_c)

    def pair(self, ident=None):
        ident = ident or self.phone
        c, session, code = self.start_pairing(ident)
        pending = self.wait_pending()
        self.assertEqual(pending["code"], code)  # os dois lados mostram o mesmo código
        c.send("pair.confirm", {"session": session, "accept": True})
        self.assertTrue(self.call(self.d.confirm(pending["pid"], True))["ok"])  # a ordem entre as duas confirmações é livre
        self.assertEqual(c.recv()["body"], {"ok": True})
        c.close()
        paired = self.client(ident)
        self.assertEqual(paired.hello(with_cert=False)["type"], "hello")
        return paired

    # ── testes ───────────────────────────────────────────────────────────
    def test_pair_reconnect_ping_and_persist(self):
        events = []
        self.d.emit = events.append
        c = self.pair()
        c.send("ping", {})
        self.assertEqual(c.recv()["type"], "pong")
        self.assertIn(self.fp_phone, Daemon(self.data, [], 0).store.records)
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[:2], ["pair.pending", "pair.result"])
        self.assertIn("device.connected", kinds)
        self.assertNotIn("cert_pem", str(events))  # eventos só com o necessário

    def test_unpaired_is_limited(self):
        c = self.client(self.phone, present=False)
        c.hello()
        c.send("ping", {})
        self.assertEqual(c.recv()["body"]["code"], "unauthorized")
        c.send("actions.list", {})
        self.assertEqual(c.recv()["body"]["code"], "unauthorized")

    def test_hello_required_and_cert_checked(self):
        c = self.client(self.phone, present=False)
        c.send("pair.request", {"commit": "0" * 64})
        self.assertEqual(c.recv()["body"]["code"], "no_hello")
        body = {"device_id": device_id(self.phone[0]), "name": "x", "kind": "phone", "app_version": "0",
                "protocol": [1], "cert_pem": self.tablet[0].read_text()}
        c.send("hello", body)
        self.assertEqual(c.recv()["body"]["code"], "bad_body")

    def test_unknown_cert_rejected(self):
        self.pair()
        c = self.client(self.evil)
        self.assertTrue(c.closed())

    def test_revoke_closes_active_session(self):
        c = self.pair()
        self.assertTrue(self.on_loop(lambda: self.d.revoke(self.fp_phone)))
        self.assertTrue(c.closed())
        again = self.client(self.phone)
        self.assertTrue(again.closed())

    def test_new_connection_replaces_old(self):
        old = self.pair()
        new = self.client(self.phone)
        new.hello(with_cert=False)
        self.assertTrue(old.closed())
        new.send("ping", {})
        self.assertEqual(new.recv()["type"], "pong")

    def test_replay(self):
        c = self.pair()
        m = make("ping", {})
        c.raw(encode(m))
        c.recv()
        c.raw(encode(m))
        self.assertEqual(c.recv()["body"]["code"], "replay")

    def test_five_invalid_close(self):
        c = self.client(self.phone, present=False)
        for _ in range(5):
            c.raw(struct.pack(">I", 3) + b"{x}")
            self.assertEqual(c.recv()["body"]["code"], "bad_frame")
        self.assertTrue(c.closed())

    def test_unknown_type_does_not_count(self):
        c = self.client(self.phone, present=False)
        for _ in range(6):
            c.raw(encode({"v": 1, "id": new_nonce()[:8], "type": "futuro", "body": {}}))
            self.assertEqual(c.recv()["body"]["code"], "unknown_type")

    def test_oversized_closes(self):
        c = self.client(self.phone, present=False)
        c.raw(struct.pack(">I", 1024 * 1024 + 1))
        self.assertTrue(c.closed())

    def test_actions_authorization(self):
        c = self.pair()
        c.send("actions.list", {})
        ids = [a["id"] for a in c.recv()["body"]["actions"]]
        self.assertIn("test.say", ids)
        self.assertNotIn("core.echo", ids)  # não é phone_safe
        c.send("actions.list", {})
        self.assertFalse(next(x for x in c.recv()["body"]["actions"] if x["id"] == "test.say")["allowed"])

        def run(action, params):
            c.send("action.run", {"action": action, "params": params})
            return c.recv()["body"]

        self.assertEqual(run("test.say", {"text": "oi"}), {"ok": False, "error": "forbidden"})
        self.assertFalse(self.on_loop(lambda: self.d.allow(self.fp_phone, "core.echo"))["ok"])
        self.assertTrue(self.on_loop(lambda: self.d.allow(self.fp_phone, "test.say"))["ok"])
        self.assertTrue(self.on_loop(lambda: self.d.allow(self.fp_phone, "test.file"))["ok"])
        evil = "; rm -rf ~ $(id) ../../etc/passwd"
        self.assertEqual(run("test.say", {"text": evil}), {"ok": True, "result": evil})
        self.assertEqual(run("core.echo", {"text": "x"})["error"], "forbidden")
        self.assertEqual(run("test.file", {"p": "/etc/passwd"})["error"], "forbidden")
        self.assertEqual(run("test.say", {"text=x": ""})["error"], "forbidden")
        self.assertEqual(run("test.say", {"outro": "x"})["error"], "InvalidParams")
        self.assertEqual(run("nao.existe", {})["error"], "forbidden")

    def test_pairing_expired(self):
        c, session, _ = self.start_pairing(self.phone)
        pid = self.wait_pending()["pid"]
        self.clock.offset = EXPIRES_IN + 1
        self.assertFalse(self.call(self.d.confirm(pid, True))["ok"])
        self.assertEqual(c.recv()["body"]["ok"], False)
        self.assertNotIn(self.fp_phone, self.d.store.records)

    def test_local_reject(self):
        c, _, _ = self.start_pairing(self.phone)
        pid = self.wait_pending()["pid"]
        self.call(self.d.confirm(pid, False))
        self.assertEqual(c.recv()["body"], {"ok": False, "reason": "recusado"})
        self.assertFalse(self.call(self.d.confirm(pid, True))["ok"])  # não reutilizável

    def test_concurrent_pairing_same_ip_busy(self):
        self.start_pairing(self.phone)
        other = self.client(self.tablet, present=False)
        other.hello()
        other.send("pair.request", {"commit": "0" * 64})
        self.assertEqual(other.recv()["body"]["code"], "busy")

    def test_pairing_rate_limit(self):
        for i in range(4):
            c = self.client(self.tablet, present=False)
            c.hello()
            c.send("pair.request", {"commit": "0" * 64})
            got = c.recv()
            if i == 3:
                self.assertEqual(got["body"]["code"], "rate_limited")
                break
            self.assertEqual(got["type"], "pair.challenge")
            c.send("pair.reveal", {"session": got["body"]["session"], "n_c": new_nonce()})
            self.assertEqual(c.recv()["body"], {"ok": False, "reason": "commit não confere"})

    def test_already_paired_cannot_repair(self):
        self.pair()
        c = self.client(self.phone, present=False)
        c.hello()
        c.send("pair.request", {"commit": "0" * 64})
        self.assertEqual(c.recv()["body"]["code"], "already_paired")

    def test_two_devices(self):
        a = self.pair(self.phone)
        b = self.pair(self.tablet)
        for c in (a, b):
            c.send("ping", {})
            self.assertEqual(c.recv()["type"], "pong")

    def test_share_text(self):
        from unittest import mock
        c = self.pair()
        with mock.patch("nexus.actions.desktop.copy_to_clipboard", return_value=True) as cp, \
                mock.patch("nexus.actions.desktop.notify") as nt:
            c.send("share.text", {"text": "$(id) oi"})
            self.assertEqual(c.recv()["body"], {"ok": True, "result": "copiado"})
        cp.assert_called_once_with("$(id) oi")
        self.assertIn("teste", nt.call_args[0][0])
        u = self.client(self.tablet, present=False)
        u.hello()
        u.send("share.text", {"text": "x"})
        self.assertEqual(u.recv()["body"]["code"], "unauthorized")

    def test_push_to_device(self):
        c = self.pair()
        cmd = lambda **req: self.call(self.d._control_cmd(req))
        self.assertTrue(cmd(cmd="send.text", device_id=self.fp_phone, text="do PC")["ok"])
        self.assertEqual(c.recv()["body"], {"text": "do PC"})
        self.assertTrue(cmd(cmd="open.url", device_id=self.fp_phone, url="https://exemplo.com/?q=1")["ok"])
        self.assertEqual(c.recv()["type"], "open.url")
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "intent://x"):
            self.assertEqual(cmd(cmd="open.url", device_id=self.fp_phone, url=bad)["error"], "só http(s)")
        self.assertEqual(cmd(cmd="send.text", device_id=self.fp_phone, text="a" * 70000)["error"], "too_large")
        self.assertEqual(cmd(cmd="send.text", device_id="0" * 64, text="x")["error"], "dispositivo desconectado")

    def test_mirror_request_from_device(self):
        from unittest import mock
        c = self.pair()
        with mock.patch("nexus.actions.desktop.mirror", return_value={"ok": True, "result": "serial"}) as m:
            c.send("mirror.request", {})
            self.assertEqual(c.recv()["body"], {"ok": True, "result": "serial"})
        m.assert_called_once_with("teste", "127.0.0.1")  # nome e IP vêm da conexão, não do pedido
        u = self.client(self.tablet, present=False)
        u.hello()
        u.send("mirror.request", {})
        self.assertEqual(u.recv()["body"]["code"], "unauthorized")

    def test_input_control_needs_permission(self):
        from unittest import mock
        self.pair()
        cmd = lambda **req: self.call(self.d._control_cmd(req))
        with mock.patch("nexus.actions.desktop.control", return_value={"ok": True, "result": "serial"}) as ctl:
            self.assertFalse(cmd(cmd="input.control", device_id=self.fp_phone)["ok"])  # desligada por padrão
            ctl.assert_not_called()
            self.assertTrue(cmd(cmd="devices.allow", device_id=self.fp_phone, action="input.control")["ok"])
            self.assertTrue(cmd(cmd="input.control", device_id=self.fp_phone)["ok"])
            ctl.assert_called_once_with("teste", "127.0.0.1")
        self.assertFalse(cmd(cmd="devices.allow", device_id=self.fp_phone, action="input.qualquer")["ok"])

    def test_input_arrangement_and_start(self):
        from unittest import mock
        self.pair()
        cmd = lambda **req: self.call(self.d._control_cmd(req))
        dev = self.fp_phone
        good = dict(cmd="input.config", device_id=dev, edge="right", monitor=[1920, 0, 1920, 1080], start=0.2, end=0.7)
        self.assertIn("posicione", cmd(cmd="input.start")["error"])  # sem permissão nem arranjo: nenhum alvo
        self.assertTrue(cmd(cmd="devices.allow", device_id=dev, action="input.control")["ok"])
        self.assertIn("posicione", cmd(cmd="input.start")["error"])
        for bad in ({"edge": "meio"}, {"monitor": [0, 0, 0, 10]}, {"monitor": "x"}, {"start": 0.7, "end": 0.2},
                    {"end": 1.5}, {"start": True}, {"monitor": [0, 0, 10**9, 10]}):
            self.assertFalse(cmd(**{**good, **bad})["ok"], bad)
        self.assertTrue(cmd(**good)["ok"])
        listed = next(d for d in cmd(cmd="devices.list")["devices"] if d["device_id"] == dev)
        self.assertEqual(listed["arrangement"], {"edge": "right", "monitor": [1920, 0, 1920, 1080], "start": 0.2, "end": 0.7})
        sink, cap = mock.MagicMock(accel=(1.0, 500.0, 3000.0, 3.0), size=(1080, 2400)), mock.MagicMock()
        with mock.patch("nexus.actions.desktop._adb_device", return_value=("7db4da6e", None)), \
                mock.patch("nexus.android_input.AndroidInput", return_value=sink) as mk_sink, \
                mock.patch("nexus.capture.Capture", return_value=cap) as mk_cap:
            sink.autoclick_on.return_value = True
            started = cmd(cmd="input.start")
            self.assertTrue(started["ok"])
            self.assertIn("clique automático", started["warning"])  # avisa, mas não bloqueia
            self.assertEqual(cmd(cmd="input.start")["error"], "a borda já está ativa")
        mk_sink.assert_called_once()
        self.assertEqual(mk_cap.call_args[0][0], [(dev, "right", ((1920, 0, 1920, 1080), 0.2, 0.7))])
        cap.start.assert_called_once()
        self.assertEqual(cmd(cmd="input.status")["targets"], [dev])
        self.assertEqual(next(d for d in cmd(cmd="devices.list")["devices"] if d["device_id"] == dev)["screen"], [1080, 2400])
        # "Usar na borda" desligado: sai na hora e fica de fora do próximo input.start
        self.assertTrue(cmd(cmd="input.config", device_id=dev, enabled=False)["ok"])
        for _ in range(100):
            if cap.stop.called:
                break
            time.sleep(0.02)
        cap.stop.assert_called_once()
        self.d._input_ended(self.d.kvm, None)
        self.assertFalse(next(d for d in cmd(cmd="devices.list")["devices"] if d["device_id"] == dev)["edge_on"])
        self.assertIn("posicione", cmd(cmd="input.start")["error"])
        self.assertTrue(cmd(cmd="input.config", device_id=dev, enabled=True)["ok"])
        with mock.patch("nexus.actions.desktop._adb_device", return_value=("7db4da6e", None)), \
                mock.patch("nexus.android_input.AndroidInput", return_value=sink), \
                mock.patch("nexus.capture.Capture", return_value=cap):
            cap.reset_mock()
            self.assertTrue(cmd(cmd="input.start")["ok"])
        # desligar a permissão tira o alvo; sem alvos, a sessão acaba
        self.assertTrue(cmd(cmd="devices.deny", device_id=dev, action="input.control")["ok"])
        for _ in range(100):
            if cap.stop.called:
                break
            time.sleep(0.02)
        cap.stop.assert_called_once()
        sink.close.assert_called()
        self.d._input_ended(self.d.kvm, None)
        self.assertFalse(cmd(cmd="input.status")["active"])

    def test_two_targets_and_one_drops(self):
        from unittest import mock
        self.pair()
        self.pair(self.tablet)
        cmd = lambda **req: self.call(self.d._control_cmd(req))
        tab = device_id(self.tablet[0])
        mon = [1920, 0, 1920, 1080]
        for dev, (a, b) in ((self.fp_phone, (0.0, 0.5)), (tab, (0.5, 1.0))):
            self.assertTrue(cmd(cmd="devices.allow", device_id=dev, action="input.control")["ok"])
            self.assertTrue(cmd(cmd="input.config", device_id=dev, edge="right", monitor=mon, start=a, end=b)["ok"])
        over = cmd(cmd="input.config", device_id=tab, edge="right", monitor=mon, start=0.3, end=0.8)
        self.assertIn("já é do", over["error"])  # trechos não se sobrepõem
        sinks = {}

        def make_sink(serial, on_lost, on_rotate):
            s = mock.MagicMock(accel=(1.0, 500.0, 3000.0, 3.0), size=(1080, 2400))
            s.autoclick_on.return_value = False
            sinks[serial] = (s, on_lost)
            return s
        cap = mock.MagicMock()
        with mock.patch("nexus.actions.desktop._adb_device", side_effect=lambda name, ip: ("S%d" % (len(sinks) + 1), None)), \
                mock.patch("nexus.android_input.AndroidInput", side_effect=make_sink), \
                mock.patch("nexus.capture.Capture", return_value=cap) as mk_cap:
            self.assertTrue(cmd(cmd="input.start")["ok"])
        self.assertEqual(len(mk_cap.call_args[0][0]), 2)  # uma barreira por alvo, na mesma sessão
        self.assertEqual(sorted(cmd(cmd="input.status")["targets"]), sorted([self.fp_phone, tab]))
        s2, lost2 = sinks["S2"]
        lost2()  # o segundo aparelho caiu
        self.assertEqual(len(cmd(cmd="input.status")["targets"]), 1)
        self.assertEqual(len(cap.set_regions.call_args[0][0]), 1)  # só a barreira dele saiu
        cap.stop.assert_not_called()  # os outros continuam
        self.assertTrue(cmd(cmd="input.release")["ok"])
        cap.stop.assert_called_once()

    def _fake_bt(self):
        test = self

        class FakePeriph:
            def __init__(self):
                self.on, self.watches, self.pairs, self.sent = False, {}, [], []

            def start(self):
                pass

            def connected(self, addr):
                return self.on

            def watch(self, addr, cb):
                self.watches[addr] = cb

            def pair(self, confirm, bonded, end):
                self.pairs.append((confirm, bonded, end))

            def send(self, which, report):
                self.sent.append(which)
        periph = FakePeriph()
        self.d.bt_factory = lambda: periph  # nunca o BlueZ de verdade nos testes
        return periph

    def test_bluetooth_transport_at_most_one(self):
        self.pair()
        self.pair(self.tablet)
        self._fake_bt()
        cmd = lambda **req: self.call(self.d._control_cmd(req))
        tab = device_id(self.tablet[0])
        self.assertFalse(cmd(cmd="input.config", device_id=self.fp_phone, transport="usb")["ok"])
        self.assertTrue(cmd(cmd="input.config", device_id=self.fp_phone, transport="bluetooth")["ok"])
        self.assertIn("só um aparelho", cmd(cmd="input.config", device_id=tab, transport="bluetooth")["error"])
        self.assertTrue(cmd(cmd="input.config", device_id=self.fp_phone, transport="adb")["ok"])
        self.assertTrue(cmd(cmd="input.config", device_id=tab, transport="bluetooth")["ok"])  # agora pode
        listed = {d["device_id"]: d["transport"] for d in cmd(cmd="devices.list")["devices"]}
        self.assertEqual((listed[self.fp_phone], listed[tab]), ("adb", "bluetooth"))

    def test_bluetooth_sink_chosen_and_pairing(self):
        from unittest import mock
        from nexus import bthid
        self.pair()
        periph = self._fake_bt()
        cmd = lambda **req: self.call(self.d._control_cmd(req))
        dev = self.fp_phone
        events = []
        self.d.emit = events.append
        self.assertTrue(cmd(cmd="devices.allow", device_id=dev, action="input.control")["ok"])
        self.assertTrue(cmd(cmd="input.config", device_id=dev, transport="bluetooth", edge="right",
                            monitor=[0, 0, 1920, 1080], start=0.2, end=0.7)["ok"])
        # pareamento: a confirmação vai ao app (evento) e volta pelo comando bt.confirm
        self.assertTrue(cmd(cmd="input.bt_pair", device_id=dev)["ok"])
        confirm, bonded, _end = periph.pairs[0]
        answer = []
        t = threading.Thread(target=lambda: answer.append(confirm("POCO", "123456")))
        t.start()
        for _ in range(100):
            ask = [e for e in events if e.get("event") == "bt.confirm"]
            if ask:
                break
            time.sleep(0.02)
        self.assertEqual(ask[0]["code"], "123456")
        self.assertTrue(cmd(cmd="bt.confirm", pid=ask[0]["pid"], accept=True)["ok"])
        t.join(5)
        self.assertEqual(answer, [True])
        bonded("AA:BB:CC:DD:EE:FF")
        for _ in range(100):
            if self.d.store.records[dev].get("bt_address"):
                break
            time.sleep(0.02)
        self.assertEqual(self.d.store.records[dev]["bt_address"], "AA:BB:CC:DD:EE:FF")
        self.d.store.update(dev, "screen", [1080, 2400])
        self.d.store.update(dev, "accel", [1.0, 500.0, 3000.0, 3.0])
        cap = mock.MagicMock()
        with mock.patch("nexus.actions.desktop._adb_device", side_effect=AssertionError("sem adb no Bluetooth")), \
                mock.patch("nexus.capture.Capture", return_value=cap):
            self.assertIn("não está conectado", cmd(cmd="input.start")["error"])  # desconectado: sem barreira
            periph.on = True
            self.assertTrue(cmd(cmd="input.start")["ok"])
        sink = self.d.kvm["mux"].targets[dev].sink
        self.assertIsInstance(sink, bthid.BtHid)
        self.assertEqual((sink.size, sink.accel), ((1080, 2400), (1.0, 500.0, 3000.0, 3.0)))
        # desconectou: a barreira sai; conectou de novo: volta
        self.on_loop(lambda: self.d._bt_changed(dev, False))
        self.assertNotIn(dev, self.d.kvm["mux"].targets)
        with mock.patch("nexus.capture.Capture", return_value=cap):
            self.on_loop(lambda: self.d._bt_changed(dev, True))
            for _ in range(100):
                if dev in self.d.kvm["mux"].targets:
                    break
                time.sleep(0.02)
        self.assertIn(dev, self.d.kvm["mux"].targets)
        self.assertEqual(len(cap.set_regions.call_args[0][0]), 1)

    def test_audio_stream(self):
        import struct as st
        import sys
        from unittest import mock
        c = self.pair()
        zeros = [sys.executable, "-c", "import sys,time\nwhile True: sys.stdout.buffer.write(bytes(1920)); sys.stdout.flush(); time.sleep(0.01)"]
        with mock.patch("nexus.audio.start", return_value=(1, 2)) as start, \
                mock.patch("nexus.audio.stop") as stop, mock.patch("nexus.audio.capture_argv", return_value=zeros):
            a = self.client(self.phone)
            a.send("audio.attach", {})
            ready = a.recv()
            self.assertEqual(ready["body"], {"rate": 48000, "channels": 2, "format": "s16le"})
            n = st.unpack(">I", a._recv(4))[0]
            self.assertEqual(n, 1920)
            self.assertEqual(a._recv(n), bytes(1920))
            self.assertEqual(self.call(self.d._control_cmd({"cmd": "devices.list"}))["audio"], self.fp_phone)
            c.send("ping", {})  # a conexão de controle continua de pé
            self.assertEqual(c.recv()["type"], "pong")
            self.assertEqual(self.call(self.d._control_cmd({"cmd": "audio.stop"})), {"ok": True})
            self.assertTrue(a.closed())
            for _ in range(100):
                if stop.called:
                    break
                time.sleep(0.02)
            stop.assert_called_once_with(1, 2)  # saída padrão restaurada
            start.assert_called_once_with("teste", True)
        u = self.client(self.tablet, present=False)
        u.send("audio.attach", {})
        self.assertEqual(u.recv()["body"]["code"], "unauthorized")

    def test_rename(self):
        self.pair()
        cmd = lambda **req: self.call(self.d._control_cmd(req))
        name = lambda: next(d for d in cmd(cmd="devices.list")["devices"] if d["device_id"] == self.fp_phone)["name"]
        self.assertTrue(cmd(cmd="devices.rename", device_id=self.fp_phone, name="  POCO   X5 Pro  ")["ok"])
        self.assertEqual(name(), "POCO X5 Pro")
        self.assertEqual(self.d.store.records[self.fp_phone]["name"], "teste")  # o nome do aparelho fica guardado
        self.assertTrue(cmd(cmd="devices.rename", device_id=self.fp_phone, name="")["ok"])
        self.assertEqual(name(), "teste")  # vazio volta ao nome do aparelho
        self.assertFalse(cmd(cmd="devices.rename", device_id="x" * 64, name="a")["ok"])

    def test_audio_push(self):
        import struct as st
        import sys
        from unittest import mock
        events = []
        self.d.emit = events.append
        c = self.pair()
        out = Path(self.tmp.name) / "tocado.raw"
        sink = [sys.executable, "-c", f"import sys\nf = open({str(out)!r}, 'wb', buffering=0)\n"
                "while b := sys.stdin.buffer.read1(65536): f.write(b)"]
        fmt = {"rate": 48000, "channels": 2, "format": "s16le"}
        answers = [None, "sempre"]
        with mock.patch("nexus.audio.playback_argv", return_value=sink) as argv, \
                mock.patch("nexus.actions.desktop.ask", side_effect=lambda *a: answers.pop(0)) as ask:
            a = self.client(self.phone)
            self.assertNotIn("audio.in", self.d.store.records[self.fp_phone]["permissions"])  # nasce desligada
            a.send("audio.push", fmt)
            self.assertEqual(a.recv()["body"]["code"], "forbidden")  # o PC perguntou e o usuário negou
            ask.assert_called_once_with("Áudio de teste", "teste quer tocar o áudio dele neste PC.", 60)
            # o PC pede: vale uma vez, sem a permissão
            self.assertTrue(self.call(self.d._control_cmd({"cmd": "audio.in.start", "device_id": self.fp_phone}))["ok"])
            self.assertEqual(c.recv()["type"], "audio.in.start")
            a = self.client(self.phone)
            a.send("audio.push", {**fmt, "rate": 44100})
            self.assertEqual(a.recv()["body"]["code"], "bad_format")
            a = self.client(self.phone)
            a.send("audio.push", fmt)  # usa o pedido do PC, sem perguntar
            self.assertEqual(a.recv()["body"], fmt)
            self.assertEqual(ask.call_count, 1)
            for i in range(3):
                a.raw(st.pack(">I", 1920) + bytes([i]) * 1920)
            for _ in range(100):
                if self.call(self.d._control_cmd({"cmd": "devices.list"}))["audio_in"]:
                    break
                time.sleep(0.02)
            self.assertEqual(self.call(self.d._control_cmd({"cmd": "devices.list"}))["audio_in"], [self.fp_phone])
            self.assertEqual(argv.call_args[0][:2], ("teste", self.fp_phone[:12]))
            for _ in range(100):
                if out.exists() and out.stat().st_size == 3 * 1920:
                    break
                time.sleep(0.02)
            self.assertTrue(self.call(self.d._control_cmd({"cmd": "audio.in.stop", "device_id": self.fp_phone}))["ok"])
            self.assertTrue(a.closed())
            self.assertEqual(out.read_bytes(), bytes(1920) + b"\x01" * 1920 + b"\x02" * 1920)
            for _ in range(100):
                if not self.call(self.d._control_cmd({"cmd": "devices.list"}))["audio_in"]:
                    break
                time.sleep(0.02)
            self.assertEqual(self.call(self.d._control_cmd({"cmd": "devices.list"}))["audio_in"], [])
            b = self.client(self.phone)
            b.send("audio.push", fmt)  # o pedido do PC já foi usado: pergunta de novo, e agora "Sempre permitir"
            self.assertEqual(b.recv()["body"], fmt)
            self.assertIn("audio.in", self.d.store.records[self.fp_phone]["permissions"])
            b.s.close()
        self.assertIn({"event": "audio.in.changed", "device_id": self.fp_phone, "on": False}, events)

    def test_relay_between_devices(self):
        import struct as st
        from unittest import mock
        from nexus.proto.identity import device_id
        c = self.pair()
        t = self.pair(self.tablet)
        fp_tablet = device_id(self.tablet[0])
        c.send("devices.peers", {})
        peers = c.recv()["body"]["devices"]
        self.assertEqual([(p["device_id"], p["connected"]) for p in peers], [(fp_tablet, True)])  # sem o próprio
        answers = [None, "sempre"]
        down = Path(tempfile.mkdtemp(dir=self.tmp.name))
        with mock.patch("nexus.actions.desktop.ask", side_effect=lambda *a: answers.pop(0)) as ask, \
                mock.patch("nexus.files.download_dir", return_value=down):
            relay = lambda **b: (c.send("relay", b), c.recv()["body"])[1]
            self.assertFalse(relay(to=fp_tablet, kind="text", value="oi")["ok"])  # perguntou e o usuário negou
            self.assertEqual(relay(to=fp_tablet, kind="text", value="oi"), {"ok": True})  # "Sempre permitir"
            self.assertIn("devices.relay", self.d.store.records[self.fp_phone]["permissions"])
            self.assertEqual(t.recv()["body"], {"text": "oi", "from": "teste"})
            self.assertFalse(relay(to=fp_tablet, kind="url", value="javascript:alert(1)")["ok"])
            self.assertTrue(relay(to=fp_tablet, kind="url", value="https://exemplo.org")["ok"])
            self.assertEqual(t.recv()["body"], {"url": "https://exemplo.org", "from": "teste"})
            self.assertFalse(relay(to=self.fp_phone, kind="text", value="eu")["ok"])  # para si mesmo, não
            self.assertEqual(ask.call_count, 2)  # com a permissão, não pergunta mais
            a = self.client(self.phone)
            a.send("file.push", {"name": "nota.txt", "size": 3, "to": fp_tablet})
            self.assertEqual(a.recv()["type"], "file.ready")
            a.raw(st.pack(">I", 3) + b"abc")
            self.assertTrue(a.recv()["body"]["ok"])
            offer = t.recv()
            self.assertEqual((offer["type"], offer["body"]["name"], offer["body"]["from"]), ("file.offer", "nota.txt", "teste"))
        t.send("unpair", {})
        for _ in range(100):
            if fp_tablet not in self.d.by_device:
                break
            time.sleep(0.02)
        c.send("relay", {"to": fp_tablet, "kind": "text", "value": "oi"})
        self.assertFalse(c.recv()["body"]["ok"])  # revogado: não pareado

    def test_audio_to_other_device(self):
        import struct as st
        import sys
        from unittest import mock
        from nexus.proto.identity import device_id
        c = self.pair()
        t = self.pair(self.tablet)
        fp_tablet = device_id(self.tablet[0])
        self.assertTrue(self.call(self.d._control_cmd({"cmd": "devices.allow", "device_id": self.fp_phone,
                                                         "action": "devices.relay"}))["ok"])
        zeros = [sys.executable, "-c", "import sys,time\nwhile True: sys.stdout.buffer.write(bytes(1920)); sys.stdout.flush(); time.sleep(0.01)"]
        sink = [sys.executable, "-c", "import sys\nwhile sys.stdin.buffer.read1(65536): pass"]
        fmt = {"rate": 48000, "channels": 2, "format": "s16le"}
        with mock.patch("nexus.audio.start", return_value=(7, None)) as start, mock.patch("nexus.audio.stop"), \
                mock.patch("nexus.audio.capture_argv", return_value=zeros) as capture, \
                mock.patch("nexus.audio.playback_argv", return_value=sink) as argv:
            a = self.client(self.phone)
            a.send("audio.push", {**fmt, "to": fp_tablet})
            self.assertEqual(t.recv()["type"], "audio.start")  # o PC liga o áudio no tablet
            tb = self.client(self.tablet)
            tb.send("audio.attach", {})
            self.assertEqual(tb.recv()["type"], "audio.ready")
            self.assertEqual(a.recv()["body"], fmt)
            start.assert_called_once_with("teste", False)  # a saída do tablet não vira a padrão do PC
            capture.assert_called_once_with("nexus-ponte")  # nem tem o nome que o WirePlumber lembra como padrão
            self.assertEqual(argv.call_args[0][2], "nexus-ponte")  # o celular toca na saída do tablet (pelo nome)
            a.raw(st.pack(">I", 1920) + bytes(1920))
            a.s.close()
            for _ in range(200):  # o celular parou: o tablet, que só ouvia por isso, para junto
                if self.d.audio_conn is None:
                    break
                time.sleep(0.02)
            self.assertIsNone(self.d.audio_conn)
            ended = False
            try:  # o tablet ainda tem quadros no caminho: lê até a conexão acabar
                while tb.s.recv(65536):
                    pass
                ended = True
            except OSError:
                ended = True
            self.assertTrue(ended)
        c.send("ping", {})
        self.assertEqual(c.recv()["type"], "pong")

    def test_file_push_and_pull(self):
        import struct as st
        from unittest import mock
        events = []
        self.d.emit = events.append
        c = self.pair()
        down = Path(tempfile.mkdtemp(dir=self.tmp.name))
        with mock.patch("nexus.files.download_dir", return_value=down), mock.patch("nexus.actions.desktop.notify"):
            data = b"x" * 300000
            a = self.client(self.phone)
            a.send("file.push", {"name": "../../segredo.txt", "size": len(data)})
            self.assertEqual(a.recv()["type"], "file.ready")
            for i in range(0, len(data), 100000):
                a.raw(st.pack(">I", 100000) + data[i:i + 100000])
            self.assertEqual(a.recv()["body"], {"ok": True, "result": "segredo.txt"})
            self.assertEqual((down / "segredo.txt").read_bytes(), data)
            prog = [e for e in events if e["event"] == "file.progress"]
            self.assertEqual((prog[-1]["done"], prog[-1]["total"]), (len(data), len(data)))
            self.assertTrue(any(e["event"] == "file.done" and e["ok"] for e in events))
            # cancelar no meio: a conexão cai, o .part some e o fim é ok=False
            k = self.client(self.phone)
            k.send("file.push", {"name": "grande.bin", "size": 10_000_000})
            k.recv()
            k.raw(st.pack(">I", 100000) + bytes(100000))
            for _ in range(100):
                if self.d.transfers:
                    break
                time.sleep(0.02)
            tid = next(iter(self.d.transfers))
            self.assertTrue(self.call(self.d._control_cmd({"cmd": "file.cancel", "id": tid}))["ok"])
            self.assertTrue(k.closed())
            for _ in range(100):
                if any(e["event"] == "file.done" and not e["ok"] for e in events):
                    break
                time.sleep(0.02)
            self.assertTrue(any(e["event"] == "file.done" and not e["ok"] for e in events))
            self.assertFalse(self.d.transfers)
            # anunciou 10 bytes e mandou 20: rejeita e não deixa resto
            b = self.client(self.phone)
            b.send("file.push", {"name": "x.bin", "size": 10})
            b.recv()
            b.raw(st.pack(">I", 20) + bytes(20))
            self.assertEqual(b.recv()["body"]["code"], "bad_frame")
            for _ in range(100):  # o .part do envio cancelado some no finally
                if sorted(p.name for p in down.iterdir()) == ["segredo.txt"]:
                    break
                time.sleep(0.02)
            self.assertEqual(sorted(p.name for p in down.iterdir()), ["segredo.txt"])
        # PC → dispositivo
        src = down / "segredo.txt"
        self.assertTrue(self.call(self.d._control_cmd({"cmd": "send.file", "device_id": self.fp_phone, "path": str(src)}))["ok"])
        offer = c.recv()["body"]
        self.assertEqual((offer["name"], offer["size"]), ("segredo.txt", 300000))
        u = self.client(self.tablet, present=False)  # não pareado não busca
        u.send("file.pull", {"id": offer["id"]})
        self.assertEqual(u.recv()["body"]["code"], "unauthorized")
        p = self.client(self.phone)
        p.send("file.pull", {"id": offer["id"]})
        self.assertEqual(p.recv()["body"], {"name": "segredo.txt", "size": 300000})
        got = b""
        while len(got) < 300000:
            got += p._recv(st.unpack(">I", p._recv(4))[0])
        self.assertEqual(got, data)
        p.close()
        again = self.client(self.phone)  # oferta é de uso único
        again.send("file.pull", {"id": offer["id"]})
        self.assertEqual(again.recv()["body"]["code"], "unauthorized")

    def test_ui_open(self):
        from unittest import mock
        self.pair()
        with mock.patch("subprocess.Popen") as popen:
            ok = self.call(self.d._control_cmd({"cmd": "ui.open", "what": "send-file", "device_id": self.fp_phone}))
            self.assertTrue(ok["ok"])
            self.assertEqual(popen.call_args[0][0][-2:], ["send-file", self.fp_phone])
            bad = self.call(self.d._control_cmd({"cmd": "ui.open", "what": "send-file", "device_id": "; rm -rf ~"}))
            self.assertFalse(bad["ok"])  # só ids de dispositivos pareados viram argumento
            self.assertFalse(self.call(self.d._control_cmd({"cmd": "ui.open", "what": "bash"}))["ok"])
            self.assertEqual(popen.call_count, 1)

    def test_control_socket(self):
        path = self.data / "run" / "control.sock"
        self.call(self.d.start_control(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        resp = control({"cmd": "status"}, path)
        self.assertEqual(resp["device_id"], self.d.fp_s)
        self.assertFalse(control({"cmd": "x"}, path)["ok"])


if __name__ == "__main__":
    unittest.main()
