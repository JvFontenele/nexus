"""App Nexus para Linux (GTK4/libadwaita). Só fala com o nexusd pelo D-Bus da sessão: sem rede e sem chaves aqui."""
import json
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from nexus.dbus import IFACE, NAME, PATH  # noqa: E402

APP_ID = "dev.nexus.App"


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


CSS = """
window.nexus, .nx-bg { background-color: #0B0C10; color: #EDEEF2; }
.nx-side { background-color: #111317; border-right: 1px solid #23262E; }
.nx-side-title { font-size: 0.8em; letter-spacing: 0.08em; color: #8E929D; }
.nx-device { border-radius: 12px; padding: 8px 10px; }
.nx-device.selected { background-color: #222B45; }
.nx-device-status { font-size: 0.85em; color: #A7AAB4; }
.nx-device-status.on { color: #9FE3C1; }
.nx-pair-btn { border-radius: 12px; border: 1.5px dashed #3E4352; background: none; min-height: 44px; }
.nx-foot { font-size: 0.85em; color: #8E929D; }
.nx-card { background-color: #16181D; border-radius: 22px; padding: 18px; }
.nx-hero-name { font-size: 1.7em; font-weight: 700; }
.nx-hero-sub { color: #B9BCC6; }
.nx-ctl { border-radius: 18px; border: 1.5px solid #343845; background-color: #16181D; min-height: 84px; }
.nx-ctl:hover { background-color: #1D2027; }
.nx-ctl.active { border-color: #8EA8FF; background-color: #222B45; }
.nx-ctl:disabled { border-color: #23262E; color: #6B6F7A; }
.nx-blue { color: #A9BCFF; } .nx-orange { color: #FFC58A; } .nx-green { color: #7FE0B0; } .nx-purple { color: #CDA9FF; }
.nx-h2 { font-weight: 700; font-size: 1.05em; }
.nx-row { padding: 8px 0; border-bottom: 1px solid #23262E; }
.nx-muted { color: #A7AAB4; }
.nx-danger { color: #FF9B8E; }
"""


def mascot_area(width: int, height: int, expr_fn) -> Gtk.DrawingArea:
    from nexus import mascote
    area = Gtk.DrawingArea(content_width=width, content_height=height, valign=Gtk.Align.CENTER)
    area.set_draw_func(lambda _a, cr, w, h: mascote.desenhar(cr, w, h, expr_fn()))
    return area


def activity(device_id: str, limit: int = 6) -> list[tuple[str, str]]:
    """Últimos eventos do dispositivo no log local (só chaves e nomes de ação: o log nunca guarda conteúdo)."""
    import datetime
    from nexus.log import state_dir
    labels = {"file.received": "Arquivo recebido", "file.sent": "Arquivo enviado", "share.text": "Texto do dispositivo",
              "send.text": "Texto enviado", "open.url": "Link aberto no dispositivo", "audio.start": "Áudio no dispositivo",
              "pair.ok": "Pareado"}
    try:
        lines = (state_dir() / "events.jsonl").read_text().splitlines()[-400:]
    except OSError:
        return []
    out = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("device_id") != device_id:
            continue
        kind = ev.get("event")
        if kind == "remote.action":
            label = f"{ev.get('action')} {'executada' if ev.get('ok') else 'negada'}"
        elif kind in labels:
            label = labels[kind]
        else:
            continue
        mins = int((now - datetime.datetime.fromisoformat(ev["ts"])).total_seconds() // 60)
        when = "agora" if mins < 1 else f"{mins} min" if mins < 60 else f"{mins // 60} h" if mins < 1440 else f"{mins // 1440} d"
        out.append((label, when))
        if len(out) >= limit:
            break
    return out


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Nexus", default_width=1120, default_height=760)
        self.add_css_class("nexus")
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.banner = Adw.Banner(button_label="Cancelar")  # progresso de arquivo (o notch mostra o mesmo)
        self.banner.connect("button-clicked", lambda _b: self.transfer and self.cmd({"cmd": "file.cancel", "id": self.transfer}))
        view.add_top_bar(self.banner)
        self.transfer = None
        self.status = Adw.StatusPage(icon_name="network-offline-symbolic", title="nexusd não está rodando",
                                     description="Inicie com: systemctl --user start nexusd")
        body = Gtk.Box()
        self.side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=260,
                            margin_top=16, margin_bottom=12, margin_start=12, margin_end=12)
        side_wrap = Gtk.Box(css_classes=["nx-side"])
        side_wrap.append(self.side)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=22, margin_bottom=22,
                               margin_start=26, margin_end=26, hexpand=True)
        scroll = Gtk.ScrolledWindow(hexpand=True, child=self.content)
        body.append(side_wrap)
        body.append(scroll)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.status, "off")
        self.stack.add_named(body, "on")
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.selected = None
        self.devs, self.actions, self.info = {"devices": [], "connected": []}, [], {}
        self.shown_pairings = set()
        self.daemon = Daemon(self.on_event, self.on_presence)

    # ── estado ───────────────────────────────────────────────────────────
    def on_presence(self, online: bool):
        self.stack.set_visible_child_name("on" if online else "off")
        if online:
            self.daemon.command({"cmd": "status"}, lambda r: setattr(self, "info", r) or self.refresh())
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
        elif kind in ("device.connected", "device.disconnected", "audio.changed", "file.received"):
            self.refresh()
        elif kind == "text.received":
            self.toast(f"Texto de {ev.get('name')}")
        elif kind == "file.progress":
            self.transfer = ev["id"]
            pct = int(ev["done"] * 100 / ev["total"]) if ev["total"] else 100
            arrow = "de" if ev["direction"] == "in" else "para"
            self.banner.set_title(GLib.markup_escape_text(f"{ev['name']} {arrow} {ev['device_name']} · {pct}%"))
            self.banner.set_revealed(True)
        elif kind == "file.done" and ev.get("id") == self.transfer:
            self.transfer = None
            self.banner.set_revealed(False)
            self.toast(f"{ev['name']}: {'concluído' if ev.get('ok') else 'cancelado'}")
            self.refresh()

    def toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def refresh(self):
        def got(devs):
            if not devs.get("ok"):
                return
            self.devs = devs
            self.daemon.command({"cmd": "actions"}, lambda acts: (setattr(self, "actions", acts.get("actions", [])),
                                                                  self.render()))
        self.daemon.command({"cmd": "devices.list"}, got)

    def device(self, d: dict) -> dict:
        return {**d, "connected": d["device_id"] in self.devs["connected"], "audio": self.devs.get("audio") == d["device_id"]}

    # ── telas ────────────────────────────────────────────────────────────
    def render(self):
        devices = [self.device(d) for d in self.devs["devices"]]
        ids = [d["device_id"] for d in devices]
        if self.selected not in ids:
            self.selected = next((d["device_id"] for d in devices if d["connected"]), ids[0] if ids else None)
        self.render_side(devices)
        child = self.content.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.content.remove(child)
            child = nxt
        d = next((x for x in devices if x["device_id"] == self.selected), None)
        if d is None:
            page = Adw.StatusPage(icon_name="phone-symbolic", title="Nenhum dispositivo pareado",
                                  description="Abra o app Nexus no celular e toque em Parear. O código aparece aqui.")
            self.content.append(page)
            return
        self.render_hero(d)
        self.render_controls(d)
        cols = Gtk.Box(spacing=14, homogeneous=True, vexpand=True)
        cols.append(self.render_permissions(d))
        cols.append(self.render_activity(d))
        self.content.append(cols)

    def render_side(self, devices: list[dict]):
        child = self.side.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.side.remove(child)
            child = nxt
        self.side.append(Gtk.Label(label="DISPOSITIVOS", xalign=0, css_classes=["nx-side-title"], margin_start=10,
                                   margin_bottom=4))
        from nexus import mascote
        for d in devices:
            row = Gtk.Box(spacing=12)
            row.append(mascot_area(36, 28, lambda d=d: mascote.expressao(d)))
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
            texts.append(Gtk.Label(label=d["name"], xalign=0, css_classes=["heading"]))
            kind = {"phone": "celular", "tablet": "tablet"}.get(d["kind"], d["kind"])
            texts.append(Gtk.Label(label=f"{kind} · {'conectado' if d['connected'] else 'fora da rede'}", xalign=0,
                                   css_classes=["nx-device-status"] + (["on"] if d["connected"] else [])))
            row.append(texts)
            b = Gtk.Button(child=row, css_classes=["flat", "nx-device"] + (["selected"] if d["device_id"] == self.selected else []))
            b.connect("clicked", lambda _b, d=d: (setattr(self, "selected", d["device_id"]), self.render()))
            self.side.append(b)
        pair = Gtk.Button(css_classes=["nx-pair-btn"], margin_top=8)
        pair.set_child(Gtk.Label(label="+  Parear dispositivo"))
        pair.connect("clicked", lambda _b: self.explain_pairing())
        self.side.append(pair)
        spacer = Gtk.Box(vexpand=True)
        self.side.append(spacer)
        listen = ", ".join(self.info.get("listen", []))
        foot = Gtk.Label(label=f"Este PC: {self.info.get('device_id', '')[:8]}…\n{listen}", xalign=0, wrap=True,
                         css_classes=["nx-foot"], margin_start=10)
        self.side.append(foot)

    def render_hero(self, d: dict):
        from nexus import mascote
        card = Gtk.Box(spacing=20, css_classes=["nx-card"])
        card.append(mascot_area(110, 87, lambda: mascote.expressao(d)))
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER)
        texts.append(Gtk.Label(label=d["name"], xalign=0, css_classes=["nx-hero-name"]))
        kind = {"phone": "Celular", "tablet": "Tablet"}.get(d["kind"], d["kind"])
        state = "conectado" if d["connected"] else "fora da rede"
        audio = " · áudio do PC tocando aqui" if d["audio"] else ""
        texts.append(Gtk.Label(label=f"{kind} · {state}{audio}", xalign=0, css_classes=["nx-hero-sub"]))
        card.append(texts)
        self.content.append(card)

    def render_controls(self, d: dict):
        dev, on = d["device_id"], d["connected"]
        controls = [
            ("video-display-symbolic", "Espelhar", "nx-blue", True, False,
             lambda: self.cmd({"cmd": "mirror", "device_id": dev})),
            ("audio-volume-muted-symbolic" if d["audio"] else "audio-speakers-symbolic",
             "Parar áudio" if d["audio"] else "Áudio aqui", "nx-blue", on, d["audio"],
             lambda: self.cmd({"cmd": "audio.stop" if d["audio"] else "audio.start", "device_id": dev})),
            ("document-send-symbolic", "Enviar arquivo", "nx-orange", on, False, lambda: self.pick_and_send(d)),
            ("edit-paste-symbolic", "Enviar texto", "nx-green", on, False,
             lambda: self.ask_text(d, "Enviar texto", "Vai para a área de transferência do dispositivo", "send.text", "text")),
            ("insert-link-symbolic", "Abrir link", "nx-purple", on, False,
             lambda: self.ask_text(d, "Abrir link", "O dispositivo mostra uma notificação para abrir", "open.url", "url")),
        ]
        grid = Gtk.Box(spacing=10, homogeneous=True)
        for icon, label, color, enabled, active, action in controls:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER)
            box.append(Gtk.Image(icon_name=icon, pixel_size=22, css_classes=[color]))
            box.append(Gtk.Label(label=label, css_classes=["heading"]))
            b = Gtk.Button(child=box, sensitive=enabled, css_classes=["nx-ctl"] + (["active"] if active else []),
                           tooltip_text=None if enabled else "O dispositivo precisa estar conectado")
            b.update_property([Gtk.AccessibleProperty.LABEL], [label])
            b.connect("clicked", lambda _b, a=action: a())
            grid.append(b)
        self.content.append(grid)

    def render_permissions(self, d: dict) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, css_classes=["nx-card"])
        card.append(Gtk.Label(label=f"O {d['name']} pode, neste PC", xalign=0, css_classes=["nx-h2"], margin_bottom=6))
        for a in self.actions:
            row = Gtk.Box(css_classes=["nx-row"])
            row.append(Gtk.Label(label=a["description"] or a["id"], xalign=0, hexpand=True))
            sw = Gtk.Switch(active=a["id"] in d["permissions"], valign=Gtk.Align.CENTER)
            sw.update_property([Gtk.AccessibleProperty.LABEL], [a["description"] or a["id"]])
            sw.connect("notify::active", self.toggle_permission, d["device_id"], a["id"])
            row.append(sw)
            card.append(row)
        revoke = Gtk.Button(label="Revogar este dispositivo…", css_classes=["flat", "nx-danger"], halign=Gtk.Align.START,
                            valign=Gtk.Align.END, vexpand=True, margin_top=8)
        revoke.connect("clicked", lambda _b: self.confirm_revoke(d))
        card.append(revoke)
        return card

    def render_activity(self, d: dict) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["nx-card"])
        card.append(Gtk.Label(label="Atividade", xalign=0, css_classes=["nx-h2"]))
        items = activity(d["device_id"])
        if not items:
            card.append(Gtk.Label(label="Nada por aqui ainda", xalign=0, css_classes=["nx-muted"]))
        for label, when in items:
            row = Gtk.Box()
            row.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            row.append(Gtk.Label(label=when, css_classes=["nx-muted"]))
            card.append(row)
        from nexus.files import download_dir
        folder = Gtk.LinkButton(uri=download_dir().as_uri(), label="Abrir a pasta de recebidos", halign=Gtk.Align.START,
                                valign=Gtk.Align.END, vexpand=True)
        card.append(folder)
        return card

    # ── ações ────────────────────────────────────────────────────────────
    def cmd(self, req: dict):
        self.daemon.command(req, lambda r: r.get("ok") or self.toast(r.get("error", "falhou")))

    def ask_text(self, d: dict, title: str, body: str, cmd: str, key: str):
        dlg = Adw.AlertDialog(heading=f"{title} no {d['name']}", body=body)
        entry = Gtk.Entry(activates_default=True, placeholder_text="https://…" if key == "url" else "Texto")
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("send", "Enviar")
        dlg.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("send")
        dlg.connect("response", lambda _d, r: r == "send" and entry.get_text() and self.daemon.command(
            {"cmd": cmd, "device_id": d["device_id"], key: entry.get_text()},
            lambda resp: self.toast("Enviado" if resp.get("ok") else resp.get("error", "falhou"))))
        dlg.present(self)

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

    def toggle_permission(self, sw, _pspec, device_id: str, action: str):
        cmd = "devices.allow" if sw.get_active() else "devices.deny"
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

    def explain_pairing(self):
        dlg = Adw.AlertDialog(heading="Parear um dispositivo",
                              body="1. Instale e abra o app Nexus no celular ou tablet.\n"
                                   "2. Toque em Procurar e depois em Parear.\n"
                                   "3. O código aparece aqui (e no notch do Balão). Confirme só se for igual.")
        dlg.add_response("ok", "Entendi")
        dlg.present(self)

    def ask_pairing(self, p: dict):
        if p["pid"] in self.shown_pairings:
            return
        self.shown_pairings.add(p["pid"])
        dlg = Adw.AlertDialog(heading=f"{p['name']} quer parear",
                              body=f"Confirme só se o {p['name']} mostrar o mesmo código.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        box.append(mascot_area(96, 76, lambda: "atento"))
        box.append(Gtk.Label(label=f"{p['code'][:3]} {p['code'][3:]}", css_classes=["title-1", "monospace"]))
        box.append(Gtk.Label(label=f"{p['fingerprint'][:32]}\n{p['fingerprint'][32:]}", css_classes=["caption", "monospace", "nx-muted"]))
        dlg.set_extra_child(box)
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


def send_file(device_id: str) -> int:
    """Só o seletor de arquivos (aberto pelo notch do Balão): escolhe e manda ao dispositivo, sem a janela do app."""
    app = Adw.Application(application_id="dev.nexus.SendFile", flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(app):
        app.hold()
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        def chosen(dialog, res):
            try:
                files = dialog.open_multiple_finish(res)
                for i in range(files.get_n_items()):
                    req = {"cmd": "send.file", "device_id": device_id, "path": files.get_item(i).get_path()}
                    bus.call_sync(NAME, PATH, IFACE, "Command", GLib.Variant("(s)", (json.dumps(req),)),
                                  GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 10000, None)
            except GLib.Error:
                pass  # cancelado ou daemon fora do ar
            app.release()

        Gtk.FileDialog(title="Enviar ao dispositivo").open_multiple(None, None, chosen)

    app.connect("activate", activate)
    return app.run([])
