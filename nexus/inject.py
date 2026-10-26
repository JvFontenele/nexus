# TokenStore e o início da sessão RemoteDesktop são adaptados de Beamer (https://github.com/kalkman-code/beamer),
# win_app/portal.py e win_app/inject_portal.py, Copyright (C) Toby Kalkman.
# Licença: GPL-3.0; veja o arquivo LICENSE na raiz do projeto.
"""Lado controlado do PC↔PC: injeta a entrada de outro PC pelo portal RemoteDesktop + libei.

`Receiver` é o estado do que chega (foco, teclas e botões apertados, posição absoluta) e decide a volta
pela borda; `RemoteDesktop` é a sessão do portal numa thread própria. O receptor guarda o estado real das
teclas e dos botões e solta tudo ao sair do foco, na desconexão e em qualquer erro."""
import collections
import os
import queue
import select
import threading
import time
from pathlib import Path

from nexus import eiread, kvmwire
from nexus.capture import GioBus, Portal, PortalError, Refused, _outer, v

RD = "org.freedesktop.portal.RemoteDesktop"
SESSION = "org.freedesktop.portal.Session"
DEV_KEYBOARD, DEV_POINTER = 1, 2
RESIST_PX = 40
WINDOW = 1.0
TICK = 0.02
NORMAL = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}


class TokenStore:
    """Restore token do RemoteDesktop: arquivo 0600, nunca em log. Vazio = esquecido (o GNOME pergunta)."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def write(self, token: str):
        if not token:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


def clamp_to(zones, x, y):
    """O ponto mais perto dentro da união das telas."""
    best = None
    for zx, zy, zw, zh in zones:
        cx, cy = min(max(x, zx), zx + zw - 1), min(max(y, zy), zy + zh - 1)
        d = (cx - x) ** 2 + (cy - y) ** 2
        if best is None or d < best[0]:
            best = (d, cx, cy)
    return best[1], best[2]


def wall_stretches(zones, wall):
    """[(linha, início, fim)] dos trechos externos da borda `wall` de todas as telas."""
    out = []
    for z in zones:
        line = {"left": z[0], "right": z[0] + z[2] - 1, "top": z[1], "bottom": z[1] + z[3] - 1}[wall]
        out += [(line, a, b) for a, b in _outer(zones, z, wall)]
    return out


class Receiver:
    """Estado do lado controlado. `handle(registros)` devolve os registros a mandar de volta (HOME)."""

    def __init__(self, injector, zones, clock=time.monotonic):
        self.inj, self.zones, self._clock = injector, zones, clock
        self.focus = False
        self.wall = None
        self.keys, self.buttons = set(), set()
        self.pos = (0.0, 0.0)
        self._push = collections.deque()

    def _need_focus(self):
        if not self.focus:
            raise kvmwire.WireError("entrada sem ENTER")

    def handle(self, records) -> list:
        out = []
        for r in records:
            kind = r[0]
            if kind == kvmwire.ENTER:
                self._enter(kvmwire.EDGES[r[1]], r[2] / 65535)
            elif kind == kvmwire.MOVE:
                self._need_focus()
                frac = self._move(r[1], r[2])
                if frac is not None:
                    self.release_all()
                    self.focus = False
                    out.append((kvmwire.HOME, round(frac * 65535)))
            elif kind == kvmwire.KEY:
                self._need_focus()
                (self.keys.add if r[2] else self.keys.discard)(r[1])
                self._ok(self.inj.key(r[1], bool(r[2])))
            elif kind == kvmwire.BUTTON:
                self._need_focus()
                (self.buttons.add if r[2] else self.buttons.discard)(r[1])
                self._ok(self.inj.button(r[1], bool(r[2])))
            elif kind == kvmwire.WHEEL:
                self._need_focus()
                self._ok(self.inj.wheel(r[1], r[2]))
            elif kind == kvmwire.LEAVE:
                self.release_all()
                self.focus = False
        return out

    @staticmethod
    def _ok(done):
        if done is False:
            raise kvmwire.WireError("o compositor recusou a injeção")

    def _enter(self, wall, frac):
        stretches = wall_stretches(self.zones, wall)
        if not stretches:
            raise kvmwire.WireError("sem borda livre desse lado")
        lo, hi = min(s[1] for s in stretches), max(s[2] for s in stretches)
        along = lo + frac * (hi - lo - 1)
        line, a, b = min(stretches, key=lambda s: 0 if s[1] <= along < s[2] else min(abs(along - s[1]), abs(along - s[2])))
        along = min(max(along, a), b - 1)
        nx, ny = NORMAL[wall]
        inset = 2
        self.pos = (line - nx * inset, along) if wall in ("left", "right") else (along, line - ny * inset)
        self.wall, self.focus = wall, True
        self._push.clear()
        self._ok(self.inj.move_abs(*self.pos))

    def _move(self, dx, dy):
        """Move; devolve a fração ao longo da borda de entrada quando o empurrão nela vence a resistência."""
        raw = (self.pos[0] + dx, self.pos[1] + dy)
        self.pos = clamp_to(self.zones, *raw)
        self._ok(self.inj.move_abs(*self.pos))
        nx, ny = NORMAL[self.wall]
        beyond = (raw[0] - self.pos[0]) * nx + (raw[1] - self.pos[1]) * ny
        if beyond <= 0:
            if dx * nx + dy * ny < 0:
                self._push.clear()
            return None
        vertical = self.wall in ("left", "right")
        along = self.pos[1] if vertical else self.pos[0]
        on_outer = [s for s in wall_stretches(self.zones, self.wall)
                    if abs((self.pos[0] if vertical else self.pos[1]) - s[0]) <= 1 and s[1] <= along < s[2]]
        if not on_outer:
            return None
        now = self._clock()
        self._push.append((now, beyond))
        while self._push and now - self._push[0][0] > WINDOW:
            self._push.popleft()
        if sum(b for _t, b in self._push) < RESIST_PX:
            return None
        stretches = wall_stretches(self.zones, self.wall)
        lo, hi = min(s[1] for s in stretches), max(s[2] for s in stretches)
        return min(max((along - lo) / max(hi - lo - 1, 1), 0.0), 1.0)

    def release_all(self):
        """Solta tudo o que este controlador apertou. Nunca levanta: é o caminho de saída."""
        for code in sorted(self.keys):
            try:
                self.inj.key(code, False)
            except Exception:
                pass
        for code in sorted(self.buttons):
            try:
                self.inj.button(code, False)
            except Exception:
                pass
        self.keys.clear()
        self.buttons.clear()


class RemoteDesktop:
    """Sessão RemoteDesktop + emissor libei numa thread com conexão D-Bus própria. Os métodos de
    injeção são chamados de outra thread: viram trabalhos na fila e devolvem True (o erro chega por
    `on_lost`). `start()` bloqueia até a sessão estar pronta (o GNOME pode perguntar ao usuário)."""

    def __init__(self, tokens: TokenStore, on_lost=lambda reason: None, bus_factory=GioBus,
                 sender_factory=eiread.Sender, ready_seconds=120.0):
        self.tokens, self.on_lost = tokens, on_lost
        self._bus_factory, self._sender_factory = bus_factory, sender_factory
        self._jobs = queue.SimpleQueue()
        self._wake_r, self._wake_w = os.pipe()
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._error = None
        self.zones = []
        self._ready_seconds = ready_seconds
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="nexus-remote-desktop", daemon=True)
        self._thread.start()
        if not self._ready.wait(self._ready_seconds) or self._error:
            self.close()
            raise PortalError(self._error or "o GNOME não respondeu a tempo")

    def _job(self, *job):
        self._jobs.put(job)
        try:
            os.write(self._wake_w, b"j")
        except (OSError, TypeError):
            pass
        return True

    def move_abs(self, x, y):
        return self._job("move_abs", x, y)

    def key(self, code, down):
        return self._job("key", code, down)

    def button(self, code, down):
        return self._job("button", code, down)

    def wheel(self, v_, h):
        return self._job("wheel", v_, h)

    def close(self):
        self._stopping.set()
        self._job("stop")
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(5)
            for fd in (self._wake_r, self._wake_w):
                if fd is not None:
                    os.close(fd)
            self._wake_r = self._wake_w = None

    def _run(self):
        bus = sender = None
        reason = None
        try:
            bus = self._bus_factory()
            portal = Portal(bus, self._stopping.is_set)
            sender, handle = self._open(portal)
            self._loop(portal, sender, handle)
        except Refused:
            reason = "o GNOME não liberou o controle remoto"
            self.tokens.write("")
        except Exception as e:
            reason = str(e) or type(e).__name__
        finally:
            if sender is not None:
                sender.close()
            if bus is not None:
                bus.close()
            if not self._ready.is_set():
                self._error = reason or "sessão encerrada"
                self._ready.set()
            elif not self._stopping.is_set():
                self.on_lost(reason or "sessão encerrada")

    def _open(self, portal):
        res = portal.request("CreateSession", "a{sv}", lambda t: ({
            "handle_token": v("s", t), "session_handle_token": v("s", portal.token())},), iface=RD)
        handle = res["session_handle"]
        opts = {"types": v("u", DEV_KEYBOARD | DEV_POINTER), "persist_mode": v("u", 2)}
        token = self.tokens.read()
        if token:
            opts["restore_token"] = v("s", token)
        portal.request("SelectDevices", "oa{sv}", lambda t: (handle, {**opts, "handle_token": v("s", t)}), iface=RD)
        started = portal.request("Start", "osa{sv}", lambda t: (handle, "", {"handle_token": v("s", t)}), iface=RD)
        if started.get("devices", 0) & (DEV_KEYBOARD | DEV_POINTER) != DEV_KEYBOARD | DEV_POINTER:
            self.tokens.write("")  # sessão sem teclado e ponteiro não é confiável: pergunta de novo da próxima vez
            raise PortalError("o GNOME não deu teclado e ponteiro")
        self.tokens.write(started.get("restore_token", ""))
        fd = portal.bus.call_fd(RD, "ConnectToEIS", "oa{sv}", (handle, {}))
        try:
            sender = self._sender_factory(fd)
        except Exception:
            os.close(fd)
            raise
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not sender.regions():  # espera o ponteiro absoluto ficar ativo
            select.select([sender.fileno()], [], [], 0.1)
            if any(e[0] == "disconnect" for e in sender.events()):
                raise PortalError("o compositor fechou a conexão do libei")
        if not sender.regions():
            raise PortalError("o GNOME não deu ponteiro absoluto")
        self.zones = [tuple(r) for r in sender.regions()]
        self._ready.set()
        return sender, handle

    def _loop(self, portal, sender, handle):
        while not self._stopping.is_set():
            ready, _, _ = select.select([sender.fileno(), self._wake_r], [], [], TICK)
            if self._wake_r in ready:
                os.read(self._wake_r, 256)
            if sender.fileno() in ready and any(e[0] == "disconnect" for e in sender.events()):
                self.tokens.write("")
                raise PortalError("o compositor fechou a conexão do libei")
            for path, iface, member, _body in portal.signals():
                if iface == SESSION and member == "Closed" and path == handle:
                    self.tokens.write("")  # fechada por fora: não confia mais no token
                    raise PortalError("o GNOME encerrou o controle remoto")
            while not self._jobs.empty():
                kind, *args = self._jobs.get()
                if kind == "stop":
                    return
                if not getattr(sender, kind)(*args):
                    raise PortalError("o compositor recusou a injeção")
