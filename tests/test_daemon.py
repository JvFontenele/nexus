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
            start.assert_called_once_with("teste")
        u = self.client(self.tablet, present=False)
        u.send("audio.attach", {})
        self.assertEqual(u.recv()["body"]["code"], "unauthorized")

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
