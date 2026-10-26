# Copyright (C) Toby Kalkman — Beamer (https://github.com/kalkman-code/beamer), win_app/libei.py.
# Adaptado para o Nexus: o receptor (InputCapture) e o emissor (RemoteDesktop), sem keymap.
# Licença: GPL-3.0; veja o arquivo LICENSE na raiz do projeto.
"""Camada ctypes mínima sobre o libei, para receber o teclado e o ponteiro capturados pelo portal
InputCapture. Só as chamadas que o Nexus usa; o snegg é a referência das assinaturas, não uma dependência
(a API dele não é estável e não tem release). O `libei.so.1` já vem com o GNOME.

`Context.events()` devolve tuplas simples, para a captura e os testes não verem ctypes:

    ("connect",) ("disconnect",) ("seat",)
    ("device_added", dev) ("device_removed", dev) ("device_resumed", dev) ("device_paused", dev)
    ("start", dev, sequence) ("stop", dev) ("frame", dev, time_us)
    ("motion", dev, dx, dy)
    ("button", dev, code, down)          código evdev (BTN_LEFT = 0x110)
    ("scroll", dev, dx, dy)              em pixels lógicos
    ("scroll_discrete", dev, dx, dy)     em 1/120 de clique, como o libei conta
    ("scroll_stop", dev)
    ("key", dev, code, down)             código evdev

`dev` é o ponteiro do dispositivo no libei (um int), só para identificar."""
import ctypes
import ctypes.util
import os

CAP_POINTER = 1 << 0
CAP_POINTER_ABSOLUTE = 1 << 1
CAP_KEYBOARD = 1 << 2
CAP_TOUCH = 1 << 3
CAP_SCROLL = 1 << 4
CAP_BUTTON = 1 << 5
CAPS = (CAP_POINTER, CAP_POINTER_ABSOLUTE, CAP_KEYBOARD, CAP_TOUCH, CAP_SCROLL, CAP_BUTTON)

EV_CONNECT = 1
EV_DISCONNECT = 2
EV_SEAT_ADDED = 3
EV_DEVICE_ADDED = 5
EV_DEVICE_REMOVED = 6
EV_DEVICE_PAUSED = 7
EV_DEVICE_RESUMED = 8
EV_FRAME = 100
EV_START_EMULATING = 200
EV_STOP_EMULATING = 201
EV_POINTER_MOTION = 300
EV_BUTTON = 500
EV_SCROLL_DELTA = 600
EV_SCROLL_STOP = 601
EV_SCROLL_CANCEL = 602
EV_SCROLL_DISCRETE = 603
EV_KEYBOARD_KEY = 700

_lib = None


def _load():
    global _lib
    if _lib is not None:
        return _lib
    try:
        lib = ctypes.CDLL(ctypes.util.find_library("ei") or "libei.so.1")
    except OSError as exc:
        raise RuntimeError("libei não está instalado: sem ele não há captura no Wayland") from exc
    p, u32, i32, dbl, b = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_double, ctypes.c_bool
    signatures = {
        "ei_new_receiver": (p, [p]),
        "ei_new_sender": (p, [p]),
        "ei_now": (ctypes.c_uint64, [p]),
        "ei_device_ref": (p, [p]),
        "ei_device_unref": (p, [p]),
        "ei_device_has_capability": (b, [p, ctypes.c_int]),
        "ei_device_get_region": (p, [p, ctypes.c_size_t]),
        "ei_region_get_x": (u32, [p]),
        "ei_region_get_y": (u32, [p]),
        "ei_region_get_width": (u32, [p]),
        "ei_region_get_height": (u32, [p]),
        "ei_device_start_emulating": (None, [p, u32]),
        "ei_device_stop_emulating": (None, [p]),
        "ei_device_frame": (None, [p, ctypes.c_uint64]),
        "ei_device_pointer_motion": (None, [p, dbl, dbl]),
        "ei_device_pointer_motion_absolute": (None, [p, dbl, dbl]),
        "ei_device_button_button": (None, [p, u32, b]),
        "ei_device_scroll_discrete": (None, [p, i32, i32]),
        "ei_device_keyboard_key": (None, [p, u32, b]),
        "ei_unref": (p, [p]),
        "ei_configure_name": (None, [p, ctypes.c_char_p]),
        "ei_setup_backend_fd": (ctypes.c_int, [p, ctypes.c_int]),
        "ei_get_fd": (ctypes.c_int, [p]),
        "ei_dispatch": (None, [p]),
        "ei_get_event": (p, [p]),
        "ei_event_unref": (p, [p]),
        "ei_event_get_type": (ctypes.c_int, [p]),
        "ei_event_get_device": (p, [p]),
        "ei_event_get_seat": (p, [p]),
        "ei_event_get_time": (ctypes.c_uint64, [p]),
        "ei_seat_has_capability": (b, [p, ctypes.c_int]),
        "ei_event_emulating_get_sequence": (u32, [p]),
        "ei_event_pointer_get_dx": (dbl, [p]),
        "ei_event_pointer_get_dy": (dbl, [p]),
        "ei_event_button_get_button": (u32, [p]),
        "ei_event_button_get_is_press": (b, [p]),
        "ei_event_scroll_get_dx": (dbl, [p]),
        "ei_event_scroll_get_dy": (dbl, [p]),
        "ei_event_scroll_get_discrete_dx": (i32, [p]),
        "ei_event_scroll_get_discrete_dy": (i32, [p]),
        "ei_event_keyboard_get_key": (u32, [p]),
        "ei_event_keyboard_get_key_is_press": (b, [p]),
    }
    for name, (restype, argtypes) in signatures.items():
        fn = getattr(lib, name)
        fn.restype = restype
        fn.argtypes = argtypes
    if hasattr(lib, "ei_disconnect"):  # só no libei ≥ 1.3; o 1.2.1 do Ubuntu 24.04 desconecta no unref
        lib.ei_disconnect.restype, lib.ei_disconnect.argtypes = None, [p]
    # variádica, terminada por zero: os tipos vão na chamada, nunca em argtypes
    lib.ei_seat_bind_capabilities.restype = None
    _lib = lib
    return lib


class Context:
    """Uma conexão libei receptora sobre o fd do ConnectToEIS. Não é thread-safe: uma thread só."""

    def __init__(self, fd: int, name: str = "Nexus", sender: bool = False):
        lib = self._lib = _load()
        self._ei = lib.ei_new_sender(None) if sender else lib.ei_new_receiver(None)
        if not self._ei:
            raise RuntimeError("libei não criou o contexto")
        lib.ei_configure_name(self._ei, name.encode())
        result = lib.ei_setup_backend_fd(self._ei, fd)  # o libei passa a ser o dono do fd
        if result != 0:
            lib.ei_unref(self._ei)
            self._ei = None
            raise OSError(-result, f"libei recusou a conexão do portal: {os.strerror(-result)}")

    def fileno(self) -> int:
        return self._lib.ei_get_fd(self._ei)

    def events(self) -> list:
        """Despacha o que chegou no fd e devolve cada evento como tupla."""
        lib = self._lib
        lib.ei_dispatch(self._ei)
        found = []
        while True:
            event = lib.ei_get_event(self._ei)
            if not event:
                return found
            try:
                item = self._read(event)
            finally:
                lib.ei_event_unref(event)
            if item is not None:
                found.append(item)

    def _read(self, event):
        lib = self._lib
        kind = lib.ei_event_get_type(event)
        if kind == EV_CONNECT:
            return ("connect",)
        if kind == EV_DISCONNECT:
            return ("disconnect",)
        if kind == EV_SEAT_ADDED:
            # receptor: pede tudo o que o assento tem, e recebe o que o compositor capturar
            seat = lib.ei_event_get_seat(event)
            caps = [ctypes.c_int(c) for c in CAPS if lib.ei_seat_has_capability(seat, c)]
            lib.ei_seat_bind_capabilities(ctypes.c_void_p(seat), *caps, ctypes.c_void_p(None))
            return ("seat",)
        dev = lib.ei_event_get_device(event)
        if not dev:
            return None
        if kind == EV_DEVICE_ADDED:
            return ("device_added", dev)
        if kind == EV_DEVICE_REMOVED:
            return ("device_removed", dev)
        if kind == EV_DEVICE_RESUMED:
            return ("device_resumed", dev)
        if kind == EV_DEVICE_PAUSED:
            return ("device_paused", dev)
        if kind == EV_START_EMULATING:
            return ("start", dev, lib.ei_event_emulating_get_sequence(event))
        if kind == EV_STOP_EMULATING:
            return ("stop", dev)
        if kind == EV_FRAME:
            return ("frame", dev, lib.ei_event_get_time(event))
        if kind == EV_POINTER_MOTION:
            return ("motion", dev, lib.ei_event_pointer_get_dx(event), lib.ei_event_pointer_get_dy(event))
        if kind == EV_BUTTON:
            return ("button", dev, lib.ei_event_button_get_button(event), bool(lib.ei_event_button_get_is_press(event)))
        if kind == EV_SCROLL_DELTA:
            return ("scroll", dev, lib.ei_event_scroll_get_dx(event), lib.ei_event_scroll_get_dy(event))
        if kind == EV_SCROLL_DISCRETE:
            return ("scroll_discrete", dev, lib.ei_event_scroll_get_discrete_dx(event),
                    lib.ei_event_scroll_get_discrete_dy(event))
        if kind in (EV_SCROLL_STOP, EV_SCROLL_CANCEL):
            return ("scroll_stop", dev)
        if kind == EV_KEYBOARD_KEY:
            return ("key", dev, lib.ei_event_keyboard_get_key(event), bool(lib.ei_event_keyboard_get_key_is_press(event)))
        return None

    def close(self):
        if self._ei:
            if hasattr(self._lib, "ei_disconnect"):
                self._lib.ei_disconnect(self._ei)
            self._lib.ei_unref(self._ei)
            self._ei = None


class Sender:
    """Emissor (RemoteDesktop): guarda os dispositivos que o compositor dá, com referência própria, e
    injeta neles. `events()` deve ser chamado quando o fd tiver dados; os envios são da mesma thread."""

    def __init__(self, fd: int, name: str = "Nexus"):
        self.ctx = Context(fd, name, sender=True)
        self._lib = self.ctx._lib
        self.devices = {}   # ponteiro → {"caps", "regions", "on"}
        self._seq = 0

    def fileno(self):
        return self.ctx.fileno()

    def events(self) -> list:
        evs = self.ctx.events()
        lib = self._lib
        for e in evs:
            if e[0] == "device_added":
                dev = lib.ei_device_ref(e[1])
                caps = {c for c in CAPS if lib.ei_device_has_capability(dev, c)}
                regions, i = [], 0
                while True:
                    r = lib.ei_device_get_region(dev, i)
                    if not r:
                        break
                    regions.append((lib.ei_region_get_x(r), lib.ei_region_get_y(r),
                                    lib.ei_region_get_width(r), lib.ei_region_get_height(r)))
                    i += 1
                self.devices[e[1]] = {"ref": dev, "caps": caps, "regions": regions, "on": False}
            elif e[0] == "device_resumed" and e[1] in self.devices:
                self._seq += 1
                lib.ei_device_start_emulating(e[1], self._seq)
                self.devices[e[1]]["on"] = True
            elif e[0] == "device_paused" and e[1] in self.devices:
                self.devices[e[1]]["on"] = False
            elif e[0] == "device_removed" and e[1] in self.devices:
                lib.ei_device_unref(self.devices.pop(e[1])["ref"])
        return evs

    def _dev(self, cap):
        return next((p for p, d in self.devices.items() if d["on"] and cap in d["caps"]), None)

    def regions(self) -> list:
        dev = self._dev(CAP_POINTER_ABSOLUTE)
        return self.devices[dev]["regions"] if dev else []

    def _frame(self, dev):
        self._lib.ei_device_frame(dev, self._lib.ei_now(self.ctx._ei))

    def move_abs(self, x, y) -> bool:
        dev = self._dev(CAP_POINTER_ABSOLUTE)
        if dev is None:
            return False
        self._lib.ei_device_pointer_motion_absolute(dev, float(x), float(y))
        self._frame(dev)
        return True

    def move_rel(self, dx, dy) -> bool:
        dev = self._dev(CAP_POINTER)
        if dev is None:
            return False
        self._lib.ei_device_pointer_motion(dev, float(dx), float(dy))
        self._frame(dev)
        return True

    def button(self, code, down) -> bool:
        dev = self._dev(CAP_BUTTON)
        if dev is None:
            return False
        self._lib.ei_device_button_button(dev, code, bool(down))
        self._frame(dev)
        return True

    def wheel(self, v, h) -> bool:
        dev = self._dev(CAP_SCROLL)
        if dev is None:
            return False
        self._lib.ei_device_scroll_discrete(dev, int(h) * 120, int(-v) * 120)  # libei: dy > 0 desce
        self._frame(dev)
        return True

    def key(self, code, down) -> bool:
        dev = self._dev(CAP_KEYBOARD)
        if dev is None:
            return False
        self._lib.ei_device_keyboard_key(dev, code, bool(down))
        self._frame(dev)
        return True

    def close(self):
        for d in self.devices.values():
            self._lib.ei_device_unref(d["ref"])
        self.devices.clear()
        self.ctx.close()
