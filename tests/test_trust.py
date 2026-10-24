import json
import socket
import ssl
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from nexus.proto.identity import check_hello_cert, device_id, ensure_identity
from nexus.proto.trust import TrustStore, client_context, pin_ok, server_context


class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name) / "id"

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_once_and_perms(self):
        cert, key = ensure_identity(self.d)
        fp = device_id(cert)
        self.assertRegex(fp, r"^[0-9a-f]{64}$")
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.d.stat().st_mode), 0o700)
        ensure_identity(self.d)
        self.assertEqual(device_id(cert), fp)

    def test_check_hello_cert(self):
        cert, _ = ensure_identity(self.d)
        pem = cert.read_text()
        self.assertEqual(check_hello_cert(device_id(cert), pem), device_id(cert))
        with self.assertRaises(ValueError):
            check_hello_cert("0" * 64, pem)
        with self.assertRaises(ValueError):
            check_hello_cert(device_id(cert), pem + pem)
        bogus = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
        with self.assertRaises(ValueError):
            check_hello_cert("0" * 64, bogus)


class TestTrustStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "share" / "trusted.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_perms_remove(self):
        st = TrustStore(self.path)
        st.add("a" * 64, "POCO", "phone", "PEM")
        st.allow("a" * 64, "core.echo")
        st.allow("a" * 64, "core.echo")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        again = TrustStore(self.path)
        self.assertEqual(again.records["a" * 64]["permissions"], ["core.echo"])
        self.assertTrue(again.remove("a" * 64))
        self.assertFalse(again.remove("a" * 64))
        self.assertEqual(json.loads(self.path.read_text()), [])
        self.assertFalse(self.path.with_name("trusted.json.tmp").exists())

    def test_lookup_none(self):
        self.assertIsNone(TrustStore(self.path).lookup_der(None))


class TestTLS(unittest.TestCase):
    """E1 como teste: todos os certificados têm o mesmo CN (caso que poderia confundir o OpenSSL)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.srv = ensure_identity(base / "srv")
        cls.cli = ensure_identity(base / "cli")
        cls.evil = ensure_identity(base / "evil")
        cls.store = TrustStore(base / "trusted.json")
        cls.store.add(device_id(cls.cli[0]), "cli", "phone", cls.cli[0].read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def handshake(self, server_ctx, client_cert):
        """Devolve (registro visto pelo servidor | 'fail', pin do servidor ok?)."""
        lsock = socket.create_server(("127.0.0.1", 0))
        out = {}

        def serve():
            conn, _ = lsock.accept()
            try:
                with server_ctx.wrap_socket(conn, server_side=True) as t:
                    out["rec"] = self.store.lookup_der(t.getpeercert(binary_form=True))
                    t.sendall(b"k")
            except (ssl.SSLError, OSError):
                out["rec"] = "fail"
                conn.close()

        th = threading.Thread(target=serve)
        th.start()
        cctx = client_context(*client_cert) if client_cert else client_context()
        pin = None
        try:
            with cctx.wrap_socket(socket.create_connection(lsock.getsockname())) as c:
                c.recv(1)  # TLS 1.3: a recusa do cert do cliente só aparece na leitura
                pin = pin_ok(c, device_id(self.srv[0]))
        except (ssl.SSLError, OSError):
            pass
        th.join(5)
        lsock.close()
        return out["rec"], pin

    def test_paired_unpaired_unknown(self):
        ctx = server_context(*self.srv, self.store)
        rec, pin = self.handshake(ctx, self.cli)
        self.assertEqual(rec["name"], "cli")
        self.assertTrue(pin)
        rec, pin = self.handshake(ctx, None)
        self.assertIsNone(rec)
        self.assertTrue(pin)
        rec, _ = self.handshake(ctx, self.evil)
        self.assertEqual(rec, "fail")

    def test_revoked_after_context_swap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        st = TrustStore(Path(tmp.name) / "t.json")
        st.add(device_id(self.cli[0]), "cli", "phone", self.cli[0].read_text())
        old = server_context(*self.srv, st)
        st.remove(device_id(self.cli[0]))
        new = server_context(*self.srv, st)
        saved, self.store = self.store, st
        try:
            self.assertEqual(self.handshake(new, self.cli)[0], "fail")
            # contexto antigo ainda aceita o TLS, mas o store (autoridade) já não reconhece: vira não pareado
            self.assertIsNone(self.handshake(old, self.cli)[0])
        finally:
            self.store = saved

    def test_wrong_pin(self):
        ctx = server_context(*self.evil, self.store)  # "servidor" impostor
        _, pin = self.handshake(ctx, None)
        self.assertFalse(pin)


if __name__ == "__main__":
    unittest.main()
