# Partes adaptadas de Beamer (https://github.com/kalkman-code/beamer), Copyright (C) Toby Kalkman:
# win_app/portal.py (Portal.request) e win_app/capture_portal.py (_outer, _segment, _Session).
# Licença: GPL-3.0; veja o arquivo LICENSE na raiz do projeto.
"""Captura do teclado e do mouse no Wayland pelo portal InputCapture + libei (nexus/eiread.py).

O Wayland não deixa um app ver a entrada dos outros: a captura é do compositor. O Nexus põe uma
barreira na borda externa escolhida; quando o ponteiro a empurra, o GNOME captura teclado e ponteiro
e manda os eventos pelo libei até o Nexus devolver (Release). Só começa sob demanda ("Ativar borda"),
nunca na partida do daemon: o InputCapture v1 do GNOME 46 pergunta ao usuário a cada sessão.

A sessão vive numa thread com uma conexão D-Bus **própria**: fechá-la (stop, erro, processo morto) faz
o portal encerrar a sessão e o compositor devolver a entrada. Essa é a volta garantida.

`handler` (chamado na thread da captura):
    activated(point, zones)   o ponteiro bateu na barreira; point = (x, y), zones = [(x, y, w, h)]
    events(lista)             eventos do libei (tuplas de eiread) enquanto ativado
    deactivated()             o compositor encerrou a captura (Release, Disable, outra janela…)
    tick()                    a cada TICK, para relógios (resistência, toque duplo)
    ended(motivo)             a sessão acabou; motivo None = stop() pedido"""
import os
import queue
import select
import threading
import time

from nexus import eiread

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
REQUEST = "org.freedesktop.portal.Request"
INTERFACE = "org.freedesktop.portal.InputCapture"
CAP_KEYBOARD, CAP_POINTER = 1, 2
TICK = 0.02
REQUEST_SECONDS = 10.0      # para pedidos sem diálogo (zonas, barreiras)
EARLY_SECONDS = 1.0         # eventos do libei que chegam antes do sinal Activated ficam guardados até isto
DISABLED_SECONDS = 1.0      # depois de um Disabled do compositor, as barreiras voltam no mínimo após isto
EDGES = ("left", "right", "top", "bottom")
NO_ID = -1                  # Activated sem activation_id (a especificação permite): o Release não leva id


class PortalError(RuntimeError):
    pass


class Refused(PortalError):
    pass


class GioBus:
    """Conexão D-Bus privada com o barramento da sessão, num GLib.MainContext da thread que a cria."""

    def __init__(self):
        from gi.repository import Gio, GLib
        self._GLib, self._Gio = GLib, Gio
        self._ctx = GLib.MainContext.new()
        self._ctx.push_thread_default()  # sinais assinados aqui são entregues neste contexto
        addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        self._c = Gio.DBusConnection.new_for_address_sync(addr, flags, None, None)
        self.unique_name = self._c.get_unique_name()
        self._queue = []
        self._c.signal_subscribe(BUS_NAME, None, None, None, None, Gio.DBusSignalFlags.NONE,
                                 lambda _c, _s, path, iface, member, params, *_: self._queue.append(
                                     (path, iface, member, params.unpack())))

    def _args(self, sig, args):
        return self._GLib.Variant(f"({sig})", args)

    def call(self, iface, method, sig, args):
        v = self._c.call_sync(BUS_NAME, OBJECT_PATH, iface, method, self._args(sig, args), None,
                              self._Gio.DBusCallFlags.NONE, -1, None)
        return v.unpack() if v is not None else ()

    def call_fd(self, iface, method, sig, args) -> int:
        v, fds = self._c.call_with_unix_fd_list_sync(BUS_NAME, OBJECT_PATH, iface, method, self._args(sig, args),
                                                     None, self._Gio.DBusCallFlags.NONE, -1, None, None)
        return fds.get(v.unpack()[0])  # dup do fd: é nosso

    def receive(self) -> list:
        while self._ctx.iteration(False):
            pass
        out, self._queue = self._queue, []
        return out

    def close(self):
        try:
            self._c.close_sync(None)
        finally:
            self._ctx.pop_thread_default()


def v(sig, value):
    """Valor de um a{sv}."""
    from gi.repository import GLib
    return GLib.Variant(sig, value)


class Portal:
    """Chamadas e pedidos ao portal; guarda os outros sinais para signals()."""

    def __init__(self, bus, stopping):
        self.bus = bus
        self._stopping = stopping
        self._waiting = []
        self._count = 0
        self._sender = bus.unique_name.lstrip(":").replace(".", "_")

    def token(self) -> str:
        self._count += 1
        return f"nexus{self._count}"

    def request(self, method: str, sig: str, make_args, timeout: float | None = None, iface: str = INTERFACE) -> dict:
        """Chama um método respondido por Request.Response e espera. Sem timeout: um diálogo espera o
        usuário o quanto for preciso, mas o stop() encerra a espera."""
        token = self.token()
        path = f"/org/freedesktop/portal/desktop/request/{self._sender}/{token}"
        self.bus.call(iface, method, sig, make_args(token))
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            self._waiting.extend(self.bus.receive())
            for item in list(self._waiting):
                at, iface, member, body = item
                if at == path and iface == REQUEST and member == "Response":
                    self._waiting.remove(item)
                    code, results = body[0], (body[1] if len(body) > 1 else {})
                    if code == 0:
                        return results
                    if code == 1:
                        raise Refused(f"{method}: recusado pelo usuário")
                    raise PortalError(f"{method}: terminou sem resposta ({code})")
            if self._stopping():
                raise PortalError(f"{method}: abandonado (parando)")
            if deadline is not None and time.monotonic() > deadline:
                raise PortalError(f"{method}: sem resposta")
            time.sleep(TICK)

    def signals(self) -> list:
        self._waiting.extend(self.bus.receive())
        out, self._waiting = self._waiting, []
        return out


# ── barreiras (puro) ────────────────────────────────────────────────────

def _outer(zones, zone, edge):
    """Trechos [início, fim) da borda `edge` de `zone` sem outra tela além dela: a barreira fica
    por fora da união das telas."""
    x, y, w, h = zone
    if edge in ("left", "right"):
        line = x if edge == "left" else x + w
        start, end = y, y + h
        beyond = [(zy, zy + zh) for zx, zy, zw, zh in zones if (zx + zw == line if edge == "left" else zx == line)]
    else:
        line = y if edge == "top" else y + h
        start, end = x, x + w
        beyond = [(zx, zx + zw) for zx, zy, zw, zh in zones if (zy + zh == line if edge == "top" else zy == line)]
    stretches = [(start, end)]
    for low, high in beyond:
        cut = []
        for a, b in stretches:
            if high <= a or low >= b:
                cut.append((a, b))
                continue
            if a < low:
                cut.append((a, low))
            if high < b:
                cut.append((high, b))
        stretches = cut
    return stretches


def _segment(zone, edge, a, b):
    x, y, w, h = zone
    line = {"left": x, "right": x + w, "top": y, "bottom": y + h}[edge]
    return (line, a, line, b - 1) if edge in ("left", "right") else (a, line, b - 1, line)


def barriers(zones, edge, region=None) -> list:
    """Posições (x1, y1, x2, y2) das barreiras na borda externa `edge`: de todas as telas, ou só do
    trecho [start, end] (frações) da borda do monitor `region = ((x, y, w, h), start, end)`."""
    found = []
    targets = zones
    if region is not None:
        monitor, start, end = region
        targets = [z for z in zones if tuple(z) == tuple(monitor)]
        if not targets:
            raise PortalError("o monitor do arranjo não existe mais: posicione o dispositivo de novo")
    for zone in targets:
        stretches = _outer(zones, zone, edge)
        if region is not None:
            x, y, w, h = zone
            first, size = (y, h) if edge in ("left", "right") else (x, w)
            low, high = first + round(region[1] * size), first + round(region[2] * size)
            stretches = [(max(a, low), min(b, high)) for a, b in stretches if min(b, high) > max(a, low)]
        for a, b in stretches:
            seg = _segment(zone, edge, a, b)
            if seg not in found:
                found.append(seg)
    return found


# ── a sessão ────────────────────────────────────────────────────────────

class Capture:
    def __init__(self, edge: str, handler, region=None, bus_factory=GioBus, ei_factory=eiread.Context,
                 clock=time.monotonic):
        if edge not in EDGES:
            raise ValueError("borda inválida")
        self.edge, self.region = edge, region
        self.handler = handler
        self._bus_factory = bus_factory
        self._ei_factory = ei_factory
        self._clock = clock
        self._stopping = threading.Event()
        self._jobs = queue.SimpleQueue()
        self._wake_r, self._wake_w = os.pipe()
        self._thread = None
        self.zones = []

    def start(self):
        self._thread = threading.Thread(target=self._run, name="nexus-capture", daemon=True)
        self._thread.start()

    def release(self, point):
        """Devolve o ponteiro ao PC em `point` (qualquer thread)."""
        self._jobs.put(("release", (float(point[0]), float(point[1]))))
        self._wake()

    def _wake(self):
        try:
            os.write(self._wake_w, b"w")
        except (OSError, TypeError):  # já fechado
            pass

    def stop(self):
        """Encerra a sessão e espera a thread (não chame de dentro do handler)."""
        self._stopping.set()
        self._wake()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(5)
            for fd in (self._wake_r, self._wake_w):
                if fd is not None:
                    os.close(fd)
            self._wake_r = self._wake_w = None

    # na thread da captura daqui para baixo
    def _run(self):
        reason, bus, ei = None, None, None
        try:
            bus = self._bus_factory()
            self.portal = Portal(bus, self._stopping.is_set)
            ei = self._open()
            self._set_barriers()
            self._loop(ei)
        except Refused:
            reason = "o GNOME não liberou a captura do teclado e do mouse"
        except Exception as e:  # qualquer falha encerra a sessão: fechar o D-Bus devolve a entrada
            reason = str(e) or type(e).__name__
        finally:
            if ei is not None:
                ei.close()
            if bus is not None:
                bus.close()
            self.handler.ended(None if self._stopping.is_set() else reason)

    def _open(self):
        results = self.portal.request("CreateSession", "sa{sv}", lambda token: ("", {
            "handle_token": v("s", token),
            "session_handle_token": v("s", self.portal.token()),
            "capabilities": v("u", CAP_KEYBOARD | CAP_POINTER),
        }))
        self.handle = results["session_handle"]
        if results.get("capabilities", 0) & (CAP_KEYBOARD | CAP_POINTER) != CAP_KEYBOARD | CAP_POINTER:
            raise PortalError("o portal não deu teclado e ponteiro")
        fd = self.portal.bus.call_fd(INTERFACE, "ConnectToEIS", "oa{sv}", (self.handle, {}))
        try:
            return self._ei_factory(fd)
        except Exception:
            os.close(fd)
            raise

    def _set_barriers(self):
        results = self.portal.request("GetZones", "oa{sv}", lambda t: (self.handle, {"handle_token": v("s", t)}),
                                      REQUEST_SECONDS)
        self.zones = [(x, y, w, h) for w, h, x, y in results.get("zones", [])]
        positions = barriers(self.zones, self.edge, self.region)
        if not positions:
            raise PortalError("esse trecho da borda encosta em outro monitor: escolha uma borda livre")
        listed = [{"barrier_id": v("u", i + 1), "position": v("(iiii)", p)} for i, p in enumerate(positions)]
        res = self.portal.request("SetPointerBarriers", "oa{sv}aa{sv}u", lambda t: (
            self.handle, {"handle_token": v("s", t)}, listed, results.get("zone_set", 0)), REQUEST_SECONDS)
        if len(res.get("failed_barriers", [])) == len(positions):
            raise PortalError("o GNOME recusou as barreiras")
        self.portal.bus.call(INTERFACE, "Enable", "oa{sv}", (self.handle, {}))

    def _loop(self, ei):
        activation, early, early_at, disabled_at = None, [], 0.0, None
        while not self._stopping.is_set():
            ready, _, _ = select.select([ei.fileno(), self._wake_r], [], [], TICK)
            if self._wake_r in ready:
                os.read(self._wake_r, 64)
            for path, iface, member, body in self.portal.signals():
                if iface != INTERFACE or not body or body[0] != self.handle:
                    continue
                opts = body[1] if len(body) > 1 else {}
                if member == "Activated":
                    activation = opts.get("activation_id", NO_ID)
                    point = opts.get("cursor_position") or (0.0, 0.0)
                    self.handler.activated((float(point[0]), float(point[1])), self.zones)
                    if early and self._clock() - early_at < EARLY_SECONDS:
                        self.handler.events(early)
                    early = []
                elif member in ("Deactivated", "Disabled"):
                    if activation is not None or member == "Deactivated":
                        self.handler.deactivated()
                    activation = None
                    if member == "Disabled":
                        disabled_at = self._clock()
                elif member == "ZonesChanged":
                    disabled_at = self._clock()  # o mutter desabilita a sessão ao mudar de monitor
            if disabled_at is not None and self._clock() - disabled_at >= DISABLED_SECONDS:
                disabled_at = None
                self._set_barriers()
            while not self._jobs.empty():
                kind, point = self._jobs.get()
                if kind == "release":
                    opts = {"cursor_position": v("(dd)", point)}
                    if activation not in (None, NO_ID):
                        opts["activation_id"] = v("u", activation)
                    self.portal.bus.call(INTERFACE, "Release", "oa{sv}", (self.handle, opts))
                    activation = None
            if ei.fileno() in ready:
                evs = ei.events()
                if any(e[0] == "disconnect" for e in evs):
                    raise PortalError("o compositor fechou a conexão do libei")
                evs = [e for e in evs if e[0] in ("motion", "button", "key", "scroll", "scroll_discrete")]
                if evs and activation is not None:
                    self.handler.events(evs)
                elif evs:  # antes do Activated: guarda um pouco
                    if not early:
                        early_at = self._clock()
                    early = (early + evs)[-64:]
            self.handler.tick()
