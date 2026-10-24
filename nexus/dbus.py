"""API local do nexusd no D-Bus da sessão (ADR-0003): dev.nexus.Daemon1.Command(s) → s e o sinal Event(s)."""
import asyncio
import json
import os
import threading

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from nexus.log import log_event  # noqa: E402

NAME = "dev.nexus.Daemon"
PATH = "/dev/nexus/Daemon"
IFACE = "dev.nexus.Daemon1"
SHELL_NAME = "dev.nexus.Shell"  # ocupado pelo módulo Nexus do Balão
MAX_REQUEST = 65536 + 4096     # texto de até 64 KiB + envelope

XML = f"""<node><interface name="{IFACE}">
  <method name="Command"><arg type="s" name="request" direction="in"/><arg type="s" name="response" direction="out"/></method>
  <signal name="Event"><arg type="s" name="event"/></signal>
</interface></node>"""


class Bridge:
    """Thread com o loop do GLib. Comandos vão para o loop asyncio do daemon; eventos voltam como sinal."""

    def __init__(self, daemon, loop: asyncio.AbstractEventLoop, address: str | None = None):
        self.daemon, self.loop, self.address = daemon, loop, address
        self.conn = None
        self.error = None
        self._ready = threading.Event()
        self._ctx = GLib.MainContext.new()
        self._glib = GLib.MainLoop.new(self._ctx, False)
        self._thread = threading.Thread(target=self._run, name="nexus-dbus", daemon=True)

    def start(self):
        self._thread.start()
        self._ready.wait(10)
        if self.conn is None:
            raise RuntimeError(f"D-Bus da sessão indisponível: {self.error}")
        self.daemon.emit = self.emit
        self.daemon.shell_present = self.shell_present

    def stop(self):
        self.daemon.emit = lambda event: None
        self.daemon.shell_present = lambda: False
        self._ctx.invoke_full(GLib.PRIORITY_DEFAULT, self._glib.quit)
        self._thread.join(5)

    def _run(self):
        self._ctx.push_thread_default()
        try:
            if self.address:
                self.conn = Gio.DBusConnection.new_for_address_sync(
                    self.address,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None, None)
            else:
                self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
            self._reg = self.conn.register_object(PATH, info, self._on_call, None, None)
            # DO_NOT_QUEUE: um segundo nexusd não fica esperando o nome; falha logo
            reply = self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                        "RequestName", GLib.Variant("(su)", (NAME, 4)), GLib.VariantType("(u)"),
                                        Gio.DBusCallFlags.NONE, -1, None)
            if reply.unpack()[0] not in (1, 4):  # PRIMARY_OWNER / ALREADY_OWNER
                raise RuntimeError(f"{NAME} já está em uso (outro nexusd?)")
        except Exception as e:
            log_event({"event": "dbus.error", "error": type(e).__name__})
            self.error = e
            self.conn = None
            self._ready.set()
            return
        self._ready.set()
        self._glib.run()
        try:  # libera o nome: um nexusd reiniciado (ou o próximo teste) consegue registrar de novo
            self.conn.unregister_object(self._reg)
            self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ReleaseName",
                                GLib.Variant("(s)", (NAME,)), None, Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error:
            pass
        self.conn = None
        self._ctx.pop_thread_default()

    def _caller_uid(self, sender: str) -> int:
        reply = self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                    "GetConnectionUnixUser", GLib.Variant("(s)", (sender,)), GLib.VariantType("(u)"),
                                    Gio.DBusCallFlags.NONE, 5000, None)
        return reply.unpack()[0]

    def _on_call(self, conn, sender, path, iface, method, params, invocation):
        try:
            if self._caller_uid(sender) != os.getuid():
                return invocation.return_dbus_error("dev.nexus.Error.Denied", "outro usuário")
            raw = params.unpack()[0]
            if len(raw) > MAX_REQUEST:
                return invocation.return_value(GLib.Variant("(s)", (json.dumps({"ok": False, "error": "too_large"}),)))
            req = json.loads(raw)
            if not isinstance(req, dict) or not isinstance(req.get("cmd"), str):
                raise ValueError("esperado objeto com cmd")
        except (ValueError, GLib.Error) as e:
            return invocation.return_value(GLib.Variant("(s)", (json.dumps({"ok": False, "error": f"bad_request: {e}"}),)))
        log_event({"event": "dbus.command", "cmd": req["cmd"], "sender": sender})
        fut = asyncio.run_coroutine_threadsafe(self.daemon._control_cmd(req), self.loop)

        def done(f):
            try:
                resp = f.result()
            except Exception as e:  # nunca deixar o chamador sem resposta
                resp = {"ok": False, "error": type(e).__name__}
            invocation.return_value(GLib.Variant("(s)", (json.dumps(resp, ensure_ascii=False),)))

        fut.add_done_callback(done)

    def emit(self, event: dict):
        if self.conn is None:
            return
        try:
            self.conn.emit_signal(None, PATH, IFACE, "Event", GLib.Variant("(s)", (json.dumps(event, ensure_ascii=False),)))
        except GLib.Error:
            pass  # frontend fora do ar nunca afeta o daemon

    def shell_present(self) -> bool:
        if self.conn is None:
            return False
        try:
            reply = self.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                        "NameHasOwner", GLib.Variant("(s)", (SHELL_NAME,)), GLib.VariantType("(b)"),
                                        Gio.DBusCallFlags.NONE, 2000, None)
            return reply.unpack()[0]
        except GLib.Error:
            return False
