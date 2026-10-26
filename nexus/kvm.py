# A classe Press é adaptada de Beamer (https://github.com/kalkman-code/beamer), win_app/capture_portal.py,
# Copyright (C) Toby Kalkman. Licença: GPL-3.0; veja o arquivo LICENSE na raiz do projeto.
"""Travessia do teclado e do mouse do PC para um celular pela borda da tela.

É o `handler` de nexus.capture.Capture: recebe a batida na barreira e os eventos do libei, decide se
atravessa (resistência na borda) e repassa ao `sink` (nexus.android_input.AndroidInput). Volta ao PC:
- empurrando o ponteiro do celular pela borda que encara o PC (posição estimada, aproximada: o Android
  aplica a aceleração dele ao mouse HID);
- com toque duplo no Ctrl direito (a rota garantida);
- quando o adb/scrcpy cai (sink_lost) ou o compositor encerra a captura.
Sempre solta todas as teclas e botões no celular ao voltar."""
import collections
import threading
import time

RESIST_PX = 40        # empurrão contra a borda, somado em WINDOW, para atravessar ou voltar
WINDOW = 1.0
IDLE_SECONDS = 1.0    # parado na barreira por isso: devolve o ponteiro ao PC (senão a entrada fica presa)
ENTRY_PX = 40         # no celular, o cursor entra um pouco para dentro da borda (tremida não o faz voltar)
DOUBLE_TAP = 0.35     # janela do toque duplo no Ctrl direito
KEY_RIGHTCTRL = 97
PIXELS_PER_NOTCH = 10.0  # o mutter manda 10 px por clique da roda no scroll contínuo
INSET_PX = 2          # ao voltar, o ponteiro fica um pouco para dentro, longe da barreira
HORIZON = 0.1         # janela do VelocityTracker (LSQ1) do Android
STEP_COUNTS = 60      # posicionamento na entrada: passos rápidos (ganho máximo do Android, previsível)
STEP_SECONDS = 0.004
SETTLE_SECONDS = 0.15  # pausa maior que HORIZON: o próximo movimento começa com ganho 1
NORMAL = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}
OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}


def _zone_at(zones, point):
    x, y = point
    for z in zones:
        if z[0] - 1 <= x <= z[0] + z[2] and z[1] - 1 <= y <= z[1] + z[3]:
            return z
    return zones[0] if zones else (0, 0, 1, 1)


def _clamp(zone, x, y):
    zx, zy, zw, zh = zone
    return min(max(x, zx), zx + zw - 1), min(max(y, zy), zy + zh - 1)


class Push:
    """Soma do empurrão contra uma borda na última janela de WINDOW segundos."""

    def __init__(self, clock):
        self._clock = clock
        self._hits = collections.deque()

    def add(self, amount) -> float:
        now = self._clock()
        self._hits.append((now, amount))
        while self._hits and now - self._hits[0][0] > WINDOW:
            self._hits.popleft()
        return sum(a for _t, a in self._hits)

    def reset(self):
        self._hits.clear()


class Aceleracao:
    """Modelo do VelocityControl do Android: o ganho do mouse cresce com a velocidade (contagens/s),
    de `scale` abaixo de `low` até scale*acceleration acima de `high`. Os parâmetros vêm do
    `dumpsys input` do aparelho. Serve para estimar onde está o cursor do celular."""

    def __init__(self, params=(1.0, 500.0, 3000.0, 3.0), clock=time.monotonic):
        self.scale, self.low, self.high, self.acc = params
        self._clock = clock
        self._hist = collections.deque()

    def gain(self, dx, dy) -> float:
        now = self._clock()
        self._hist.append((now, dx, dy))
        while now - self._hist[0][0] > HORIZON:
            self._hist.popleft()
        speed = 0.0
        if len(self._hist) >= 2:
            span = now - self._hist[0][0]
            sx = sum(h[1] for h in list(self._hist)[1:])
            sy = sum(h[2] for h in list(self._hist)[1:])
            speed = (sx * sx + sy * sy) ** 0.5 / span if span > 0 else float("inf")
        scale = self.scale
        if speed > self.low:
            scale *= self.acc if speed >= self.high else 1 + (speed - self.low) / (self.high - self.low) * (self.acc - 1)
        return scale


def trecho(zone, edge, start=0.0, end=1.0):
    """O retângulo de `zone` cortado no trecho [start, end] da borda `edge` (a parte em que o celular encosta)."""
    zx, zy, zw, zh = zone
    if edge in ("left", "right"):
        a, b = zy + round(start * zh), zy + round(end * zh)
        return (zx, a, zw, max(b - a, 1))
    a, b = zx + round(start * zw), zx + round(end * zw)
    return (a, zy, max(b - a, 1), zh)


class Press:
    """O ponteiro preso na barreira enquanto a entrada ainda está no PC: onde está (deslizando pela
    borda) e se o movimento ainda é um empurrão."""

    def __init__(self, wall, point, zone, clock=time.monotonic):
        self.wall, self.zone, self._clock = wall, zone, clock
        self.point = _clamp(zone, *point)
        self._exact = [float(self.point[0]), float(self.point[1])]
        self.push = Push(clock)
        self.pushed = 0.0  # soma na janela
        self._last_push = clock()

    def feed(self, dx, dy) -> bool:
        """True enquanto empurra para fora ou desliza pela borda; False ao voltar para dentro da tela."""
        nx, ny = NORMAL[self.wall]
        outward = dx * nx + dy * ny
        if outward < 0:
            self._move(dx, dy)
            return False
        self._move(dx * (1 - abs(nx)), dy * (1 - abs(ny)))  # a barreira segura o resto
        if outward > 0:
            self.pushed = self.push.add(outward)
            self._last_push = self._clock()
        return True

    def _move(self, dx, dy):
        self._exact[0] += dx
        self._exact[1] += dy
        self.point = _clamp(self.zone, *self._exact)
        self._exact = [float(self.point[0]), float(self.point[1])]

    def idle(self) -> bool:
        return self._clock() - self._last_push > IDLE_SECONDS


def along(zone, wall, point) -> float:
    """Fração (0–1) da posição ao longo da borda."""
    zx, zy, zw, zh = zone
    if wall in ("left", "right"):
        return min(max((point[1] - zy) / max(zh - 1, 1), 0.0), 1.0)
    return min(max((point[0] - zx) / max(zw - 1, 1), 0.0), 1.0)


def edge_point(zone, wall, fraction, inset=0):
    """Ponto na borda `wall` de `zone` na fração dada, `inset` px para dentro."""
    zx, zy, zw, zh = zone
    if wall in ("left", "right"):
        y = zy + fraction * (zh - 1)
        return (zx + inset if wall == "left" else zx + zw - 1 - inset, y)
    x = zx + fraction * (zw - 1)
    return (x, zy + inset if wall == "top" else zy + zh - 1 - inset)


class Travessia:
    def __init__(self, edge, sink, release, speed=1.0, scroll_speed=1.0, invert_scroll=False,
                 pc_clipboard=None, set_pc_clipboard=None, on_change=lambda away, reason=None: None,
                 on_end=lambda reason: None, start=0.0, end=1.0, clock=time.monotonic, sleep=time.sleep):
        self.edge, self.sink, self._release = edge, sink, release
        self.speed, self.scroll_speed, self.invert = speed, scroll_speed, invert_scroll
        self._pc_clipboard, self._set_pc_clipboard = pc_clipboard, set_pc_clipboard
        self.on_change, self.on_end, self._clock, self._sleep = on_change, on_end, clock, sleep
        self.start, self.end = start, end
        self._accel = Aceleracao(getattr(sink, "accel", (1.0, 500.0, 3000.0, 3.0)), clock)
        self._lock = threading.RLock()
        self.press = None
        self.away = False
        self._zone = None
        self._pos = [0.0, 0.0]
        self._back = Push(clock)
        self._carry = [0.0, 0.0]
        self._wheel = [0.0, 0.0]
        self._ctrl_at = None
        self._sent_clip = None
        self._frac = 0.5

    # ── handler da Capture ──
    def activated(self, point, zones):
        with self._lock:
            self.press = Press(self.edge, point, _zone_at(zones, point), self._clock)

    def events(self, evs):
        with self._lock:
            discrete = any(e[0] == "scroll_discrete" for e in evs)
            for i, e in enumerate(evs):
                if self.press is not None:
                    if e[0] != "motion":
                        self._let_go("tecla ou botão na borda")  # o evento fica perdido, como no Beamer
                        return
                    if not self.press.feed(e[2], e[3]):
                        self._let_go("voltou para dentro")
                        return
                    if self.press.pushed >= RESIST_PX:
                        self._cross()
                elif self.away:
                    self._forward(e, discrete)

    def tick(self):
        with self._lock:
            if self.press is not None and self.press.idle():
                self._let_go("parado na borda")

    def deactivated(self):
        with self._lock:
            self.press = None
            if self.away:
                self._home("o compositor encerrou a captura", release=False)

    def ended(self, reason):
        with self._lock:
            self.press = None
            if self.away:
                self._home("sessão encerrada", release=False, clip=False)
            self.sink.close()
            self.on_end(reason)

    def sink_lost(self):
        with self._lock:
            if self.away:
                self._home("adb/scrcpy caiu", clip=False)  # sem o servidor não há clipboard do celular para buscar

    # ── por dentro ──
    def _let_go(self, reason):
        self._release(self.press.point)
        self.press = None
        self.on_change(False, f"solto: {reason}")

    def _cross(self):
        press, self.press = self.press, None
        self._zone = trecho(press.zone, self.edge, self.start, self.end)
        frac = along(self._zone, self.edge, press.point)
        w, h = self.sink.size
        # o celular fica do lado `edge` do PC: o ponteiro entra pela borda oposta dele
        self._pos = list(edge_point((0, 0, w, h), OPPOSITE[self.edge], frac, ENTRY_PX))
        self._back.reset()
        self._carry = [0.0, 0.0]
        self._ctrl_at = None
        self.away = True
        if self._pc_clipboard is not None:
            text = self._pc_clipboard()
            if text:
                self.sink.set_clipboard(text)
                self._sent_clip = text
        if getattr(self.sink, "exact", False):  # outro PC: ele posiciona e sabe a posição real
            self._frac = frac
            self.sink.enter(OPPOSITE[self.edge], frac)
        else:
            self._place(w, h)
        self.on_change(True)

    def _place(self, w, h):
        """Mouse relativo: encosta no canto mais perto e anda até o ponto de entrada. Depois da pausa, um
        passo de 1 contagem (ganho 1) e o resto em passos rápidos, com o ganho máximo do Android."""
        left, top = self._pos[0] < w / 2, self._pos[1] < h / 2
        self.sink.move(-2 * w if left else 2 * w, -2 * h if top else 2 * h)
        self._sleep(SETTLE_SECONDS)
        dx = self._pos[0] - (0 if left else w - 1)
        dy = self._pos[1] - (0 if top else h - 1)
        sx, sy = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
        self.sink.move(sx, sy)
        g = self._accel.scale * self._accel.acc
        rx, ry = (dx - sx) / g, (dy - sy) / g
        n = max(1, int(max(abs(rx), abs(ry)) // STEP_COUNTS) + 1)
        done = [0, 0]
        for i in range(1, n + 1):
            self._sleep(STEP_SECONDS)
            tx, ty = round(rx * i / n), round(ry * i / n)
            self.sink.move(tx - done[0], ty - done[1])
            done = [tx, ty]
        self._sleep(SETTLE_SECONDS)  # o movimento do usuário começa do zero no rastreador do Android
        self._accel = Aceleracao((self._accel.scale, self._accel.low, self._accel.high, self._accel.acc), self._clock)

    def remote_home(self, frac):
        """O PC alvo viu o ponteiro empurrar a borda que encara este PC, na fração `frac`."""
        with self._lock:
            if self.away:
                self._frac = frac
                self._home("empurrou a borda do outro PC")

    def _home(self, reason, release=True, clip=True):
        self.sink.release_all()
        self.away = False
        w, h = self.sink.size
        if release and self._zone is not None:
            exact = getattr(self.sink, "exact", False)
            frac = self._frac if exact else along((0, 0, w, h), OPPOSITE[self.edge], self._pos)
            self._release(edge_point(self._zone, self.edge, frac, INSET_PX))
        if clip and self._set_pc_clipboard is not None:
            text = self.sink.get_clipboard()
            if text and text != self._sent_clip:
                self._set_pc_clipboard(text)
        self.on_change(False, reason)

    def _forward(self, e, discrete):
        kind = e[0]
        if kind == "motion" and getattr(self.sink, "exact", False):
            self._carry[0] += e[2] * self.speed
            self._carry[1] += e[3] * self.speed
            mx, my = int(self._carry[0]), int(self._carry[1])
            self._carry[0] -= mx
            self._carry[1] -= my
            if mx or my:
                self.sink.move(mx, my)
        elif kind == "motion":
            self._motion(e[2] * self.speed, e[3] * self.speed)
        elif kind == "key":
            code, down = e[2], e[3]
            self.sink.key(code, down)
            if code == KEY_RIGHTCTRL and down:
                now = self._clock()
                if self._ctrl_at is not None and now - self._ctrl_at <= DOUBLE_TAP:
                    self._home("Ctrl direito duas vezes")
                    return
                self._ctrl_at = now
            elif down:
                self._ctrl_at = None  # outra tecla no meio: não é toque duplo
        elif kind == "button":
            self.sink.button(e[2], e[3])
            self._ctrl_at = None
        elif kind == "scroll_discrete":
            self._scroll(e[2] / 120.0, e[3] / 120.0)
        elif kind == "scroll" and not discrete:
            self._scroll(e[2] / PIXELS_PER_NOTCH, e[3] / PIXELS_PER_NOTCH)

    def _motion(self, dx, dy):
        self._carry[0] += dx
        self._carry[1] += dy
        mx, my = int(self._carry[0]), int(self._carry[1])
        self._carry[0] -= mx
        self._carry[1] -= my
        if not (mx or my):
            return
        w, h = self.sink.size
        g = self._accel.gain(mx, my)  # o cursor do Android anda contagens × ganho
        raw = (self._pos[0] + mx * g, self._pos[1] + my * g)
        self._pos = [min(max(raw[0], 0), w - 1), min(max(raw[1], 0), h - 1)]
        nx, ny = NORMAL[OPPOSITE[self.edge]]  # a borda do celular que encara o PC
        beyond = (raw[0] - self._pos[0]) * nx + (raw[1] - self._pos[1]) * ny  # só o que passou da borda
        if beyond > 0:
            if self._back.add(beyond) >= RESIST_PX:
                self._home("empurrou a borda do celular")
                return
        elif mx * nx + my * ny < 0:
            self._back.reset()
        self.sink.move(mx, my)

    def _scroll(self, dx, dy):
        sign = -1 if self.invert else 1
        self._wheel[0] += dx * self.scroll_speed * sign
        self._wheel[1] += dy * self.scroll_speed * sign
        h, v = int(self._wheel[0]), int(self._wheel[1])
        self._wheel[0] -= h
        self._wheel[1] -= v
        if h or v:
            self.sink.wheel(-v, h)  # libei: dy > 0 desce; HID: roda > 0 sobe
