"""App Nexus para Linux (GTK4/libadwaita). Só fala com o nexusd pelo D-Bus da sessão: sem rede e sem chaves aqui."""
import json
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from nexus.dbus import IFACE, NAME, PATH  # noqa: E402

APP_ID = "dev.nexus.App"
KINDS = {"phone": "phone-symbolic", "tablet": "tablet-symbolic", "pc": "computer-symbolic"}


class Daemon:
    """Cliente assíncrono de dev.nexus.Daemon1 (no loop do GTK)."""

    def __init__(self, on_event, on_presence):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.bus.signal_subscribe(NAME, IFACE, "Event", PATH, None, Gio.DBusSignalFlags.NONE,
                                  lambda *a: on_event(json.loads(a[5].unpack()[0])))
        Gio.bus_watch_name(Gio.BusType.SESSION, NAME, Gio.BusNameWatcherFlags.NONE,
                           lambda *a: on_presence(True), lambda *a: on_presence(False))

    def command(self, req: dict, callback=None):
        def done(bus, res):
            try:
                resp = json.loads(bus.call_finish(res).unpack()[0])
            except GLib.Error as e:
                resp = {"ok": False, "error": e.message}
            if callback:
                callback(resp)

        self.bus.call(NAME, PATH, IFACE, "Command", GLib.Variant("(s)", (json.dumps(req),)), GLib.VariantType("(s)"),
                      Gio.DBusCallFlags.NONE, 10000, None, done)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Nexus", default_width=520, default_height=640)
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage()
        self.status = Adw.StatusPage(icon_name="network-offline-symbolic", title="nexusd não está rodando",
                                     description="Inicie com: nexus daemon")
        self.stack = Gtk.Stack()
        self.stack.add_named(self.status, "off")
        self.stack.add_named(self.page, "on")
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        self.groups = []
        self.shown_pairings = set()
        self.daemon = Daemon(self.on_event, self.on_presence)

    # ── estado ───────────────────────────────────────────────────────────
    def on_presence(self, online: bool):
        self.stack.set_visible_child_name("on" if online else "off")
        if online:
            self.refresh()
            self.daemon.command({"cmd": "pair.pending"},
                                lambda r: [self.ask_pairing(p) for p in r.get("pending", [])])

    def on_event(self, ev: dict):
        kind = ev.get("event")
        if kind == "pair.pending":
            self.ask_pairing(ev)
        elif kind == "pair.result":
            self.toast(f"Pareado com {ev.get('name', 'dispositivo')}" if ev.get("ok")
                       else f"Pareamento falhou: {ev.get('reason', '')}")
            self.refresh()
        elif kind in ("device.connected", "device.disconnected", "audio.changed"):
            self.refresh()
        elif kind == "text.received":
            self.toast(f"Texto de {ev.get('name')}")

    def toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def refresh(self):
        self.daemon.command({"cmd": "devices.list"}, lambda devs: self.daemon.command(
            {"cmd": "actions"}, lambda acts: self.render(devs, acts.get("actions", []))))

    # ── telas ────────────────────────────────────────────────────────────
    def render(self, devs: dict, actions: list):
        for g in self.groups:
            self.page.remove(g)
        self.groups = []
        if not devs.get("ok"):
            return
        if not devs["devices"]:
            g = Adw.PreferencesGroup(title="Nenhum dispositivo",
                                     description="Abra o app Nexus no celular e toque em Parear.")
            self.page.add(g)
            self.groups.append(g)
        for d in devs["devices"]:
            online = d["device_id"] in devs["connected"]
            g = Adw.PreferencesGroup(title=GLib.markup_escape_text(d["name"]),
                                     description=f"{d['device_id'][:16]}… · {'conectado' if online else 'desconectado'}")
            revoke = Gtk.Button(label="Revogar", css_classes=["destructive-action", "flat"], valign=Gtk.Align.CENTER)
            revoke.connect("clicked", lambda _b, d=d: self.confirm_revoke(d))
            mirror = Gtk.Button(label="Espelhar", css_classes=["flat"], valign=Gtk.Align.CENTER,
                                tooltip_text="Abre a tela do dispositivo com o scrcpy (USB ou depuração sem fio)")
            mirror.connect("clicked", lambda _b, d=d: self.daemon.command(
                {"cmd": "mirror", "device_id": d["device_id"]},
                lambda r: r.get("ok") or self.toast(r.get("error", "falhou"))))
            buttons = Gtk.Box(spacing=6)
            if online:
                on = devs.get("audio") == d["device_id"]
                audio = Gtk.Button(label="Parar áudio" if on else "Áudio", css_classes=["flat"], valign=Gtk.Align.CENTER,
                                   tooltip_text="Toca o som do PC neste dispositivo")
                audio.connect("clicked", lambda _b, d=d, on=on: self.daemon.command(
                    {"cmd": "audio.stop" if on else "audio.start", "device_id": d["device_id"]},
                    lambda r: r.get("ok") or self.toast(r.get("error", "falhou"))))
                buttons.append(audio)
                send = Gtk.Button(label="Enviar arquivo", css_classes=["flat"], valign=Gtk.Align.CENTER)
                send.connect("clicked", lambda _b, d=d: self.pick_and_send(d))
                buttons.append(send)
            buttons.append(mirror)
            buttons.append(revoke)
            g.set_header_suffix(buttons)
            perms = Adw.ExpanderRow(title="Ações permitidas pelo dispositivo",
                                    subtitle=f"{len(d['permissions'])} de {len(actions)}", icon_name=KINDS.get(d["kind"]))
            for a in actions:
                row = Adw.SwitchRow(title=GLib.markup_escape_text(a["description"] or a["id"]), subtitle=a["id"],
                                    active=a["id"] in d["permissions"])
                row.connect("notify::active", self.toggle_permission, d["device_id"], a["id"])
                perms.add_row(row)
            g.add(perms)
            self.page.add(g)
            self.groups.append(g)

    def pick_and_send(self, d: dict):
        def chosen(dialog, res):
            try:
                files = dialog.open_multiple_finish(res)
            except GLib.Error:
                return  # cancelado
            for i in range(files.get_n_items()):
                path = files.get_item(i).get_path()
                self.daemon.command({"cmd": "send.file", "device_id": d["device_id"], "path": path},
                                    lambda r, n=path.rsplit("/", 1)[-1]: self.toast(
                                        f"{n} → {d['name']}" if r.get("ok") else r.get("error", "falhou")))

        Gtk.FileDialog(title=f"Enviar para {d['name']}").open_multiple(self, None, chosen)

    def toggle_permission(self, row, _pspec, device_id: str, action: str):
        cmd = "devices.allow" if row.get_active() else "devices.deny"
        self.daemon.command({"cmd": cmd, "device_id": device_id, "action": action},
                            lambda r: r.get("ok") or self.toast(r.get("error", "falhou")))

    def confirm_revoke(self, d: dict):
        dlg = Adw.AlertDialog(heading=f"Revogar {d['name']}?",
                              body="O dispositivo perde o acesso agora e precisa parear de novo.")
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("revoke", "Revogar")
        dlg.set_response_appearance("revoke", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: r == "revoke" and self.daemon.command(
            {"cmd": "devices.revoke", "device_id": d["device_id"]}, lambda _r: self.refresh()))
        dlg.present(self)

    def ask_pairing(self, p: dict):
        if p["pid"] in self.shown_pairings:
            return
        self.shown_pairings.add(p["pid"])
        code = f"{p['code'][:3]} {p['code'][3:]}"
        dlg = Adw.AlertDialog(
            heading=f"Parear com {p['name']}?",
            body=(f"Confirme só se o {p['name']} mostrar o mesmo código.\n\n"
                  f"Fingerprint do dispositivo:\n{p['fingerprint'][:32]}\n{p['fingerprint'][32:]}"))
        label = Gtk.Label(label=code, css_classes=["title-1"], selectable=False)
        dlg.set_extra_child(label)
        dlg.add_response("reject", "Recusar")
        dlg.add_response("accept", "Confirmar")
        dlg.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("reject")  # Enter por engano não pareia
        dlg.connect("response", lambda _d, r: self.daemon.command(
            {"cmd": "pair.confirm", "pid": p["pid"], "accept": r == "accept"},
            lambda resp: resp.get("ok") or self.toast(resp.get("error", "falhou"))))
        dlg.present(self)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        (self.props.active_window or Window(self)).present()


def main() -> int:
    return App().run(sys.argv[:1])
