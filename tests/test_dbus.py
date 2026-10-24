import asyncio
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from nexus.actions import load_all  # noqa: E402
from nexus.daemon import Daemon  # noqa: E402
from nexus.dbus import IFACE, MAX_REQUEST, NAME, PATH, SHELL_NAME, Bridge  # noqa: E402


def connect(address):
    return Gio.DBusConnection.new_for_address_sync(
        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None)


def command(conn, req) -> dict:
    raw = req if isinstance(req, str) else json.dumps(req)
    reply = conn.call_sync(NAME, PATH, IFACE, "Command", GLib.Variant("(s)", (raw,)), GLib.VariantType("(s)"),
                           Gio.DBusCallFlags.NONE, 5000, None)
    return json.loads(reply.unpack()[0])


class TestDBus(unittest.TestCase):
    """Barramento privado: nunca toca a sessão real do usuário."""

    @classmethod
    def setUpClass(cls):
        load_all()
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["NEXUS_STATE_DIR"] = str(Path(cls.tmp.name) / "state")
        cls.bus = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                                   stdout=subprocess.PIPE, text=True)
        cls.address = cls.bus.stdout.readline().strip()
        cls.loop = asyncio.new_event_loop()
        threading.Thread(target=cls.loop.run_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.bus.terminate()
        cls.bus.wait(5)
        cls.tmp.cleanup()

    def setUp(self):
        self.d = Daemon(Path(tempfile.mkdtemp(dir=self.tmp.name)), [], 0)
        self.bridge = Bridge(self.d, self.loop, self.address)
        self.bridge.start()
        self.client = connect(self.address)

    def tearDown(self):
        self.bridge.stop()
        self.client.close_sync(None)

    def test_status_and_actions(self):
        self.assertEqual(command(self.client, {"cmd": "status"})["device_id"], self.d.fp_s)
        ids = [a["id"] for a in command(self.client, {"cmd": "actions"})["actions"]]
        self.assertIn("pc.notify", ids)
        self.assertNotIn("core.echo", ids)

    def test_invalid_requests(self):
        self.assertIn("bad_request", command(self.client, "não é json")["error"])
        self.assertIn("bad_request", command(self.client, "[1]")["error"])
        self.assertIn("bad_request", command(self.client, {"cmd": 3})["error"])
        self.assertEqual(command(self.client, {"cmd": "rm -rf"})["error"], "comando desconhecido")
        big = json.dumps({"cmd": "status", "x": "a" * MAX_REQUEST})
        self.assertEqual(command(self.client, big)["error"], "too_large")

    def test_pair_confirm_unknown_or_repeated(self):
        r = command(self.client, {"cmd": "pair.confirm", "pid": "nada", "accept": True})
        self.assertFalse(r["ok"])
        self.assertFalse(command(self.client, {"cmd": "devices.allow", "device_id": "x", "action": "pc.notify"})["ok"])

    def test_events_reach_multiple_clients(self):
        other = connect(self.address)
        self.addCleanup(other.close_sync, None)
        got = {"a": [], "b": []}
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        for key, conn in (("a", self.client), ("b", other)):
            conn.signal_subscribe(NAME, IFACE, "Event", PATH, None, Gio.DBusSignalFlags.NONE,
                                  lambda *a, k=key: got[k].append(json.loads(a[5].unpack()[0])))
        self.d.emit({"event": "device.connected", "device_id": "x", "name": "POCO"})
        deadline = time.monotonic() + 3
        while (not got["a"] or not got["b"]) and time.monotonic() < deadline:
            ctx.iteration(False)
            time.sleep(0.01)
        ctx.pop_thread_default()
        self.assertEqual(got["a"][0]["name"], "POCO")
        self.assertEqual(got["b"][0]["event"], "device.connected")

    def test_shell_presence(self):
        self.assertFalse(self.d.shell_present())
        shell = connect(self.address)
        shell.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                        GLib.Variant("(su)", (SHELL_NAME, 4)), None, Gio.DBusCallFlags.NONE, 5000, None)
        self.assertTrue(self.d.shell_present())
        shell.close_sync(None)  # o frontend cai: o daemon percebe e segue funcionando
        for _ in range(100):
            if not self.d.shell_present():
                break
            time.sleep(0.02)
        self.assertFalse(self.d.shell_present())
        self.assertTrue(command(self.client, {"cmd": "status"})["ok"])

    def test_second_daemon_refused(self):
        other = Bridge(Daemon(Path(tempfile.mkdtemp(dir=self.tmp.name)), [], 0), self.loop, self.address)
        with self.assertRaises(RuntimeError):
            other.start()

    def test_stop_disables_emit(self):
        self.bridge.stop()
        self.d.emit({"event": "x"})  # não pode falhar com a ponte desligada
        self.bridge = Bridge(self.d, self.loop, self.address)
        self.bridge.start()


if __name__ == "__main__":
    unittest.main()
