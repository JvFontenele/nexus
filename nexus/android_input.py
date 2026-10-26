"""Teclado e mouse do PC no Android pelo scrcpy-server (o mesmo que o `scrcpy` instalado usa), sem root.

O Nexus envia o `scrcpy-server` da instalação local por adb, sobe-o só com controle (sem vídeo nem
áudio) e fala o protocolo de controle dele: dois dispositivos UHID (um teclado e um mouse físicos para o
Android, com o cursor do sistema), INJECT_KEYCODE para as teclas de mídia e SET/GET_CLIPBOARD.

O protocolo interno do scrcpy muda entre versões: este cliente fala com o servidor **5.0.1** e confere
a versão antes. Formatos conferidos no dex do servidor 5.0.1 (ControlMessageReader/DeviceMessageWriter).
Tudo vai como lista de argumentos fixos; o serial vem de `adb devices` (validado) e o scid é gerado aqui."""
import re
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

SERVER_VERSION = "5.0.1"
REMOTE_PATH = "/data/local/tmp/nexus-scrcpy-server.jar"
_SERIAL = re.compile(r"[A-Za-z0-9._:-]{1,64}")
CONNECT_SECONDS = 8.0
ROTATION_SECONDS = 2.0
MAX_CLIPBOARD = 1 << 20  # o servidor limita a menos que isto; acima, é lixo e a conexão cai

# mensagens de controle (cliente → servidor)
INJECT_KEYCODE, GET_CLIPBOARD, SET_CLIPBOARD, UHID_CREATE, UHID_INPUT, UHID_DESTROY = 0, 8, 9, 12, 13, 14
# mensagens do dispositivo (servidor → cliente)
DEV_CLIPBOARD, DEV_ACK_CLIPBOARD, DEV_UHID_OUTPUT = 0, 1, 2
KEYBOARD_ID, MOUSE_ID = 1, 2

# Descritores HID padrão (especificação USB HID 1.11): teclado de 8 bytes com LEDs; mouse com 5 botões,
# X/Y relativos de 16 bits, roda vertical e horizontal (AC Pan). Relatórios sem report ID.
KEYBOARD_DESC = bytes([
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,
    0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x95, 0x05, 0x75, 0x01, 0x91, 0x02,
    0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
    0x05, 0x07, 0x19, 0x00, 0x29, 0xE7, 0x15, 0x00, 0x26, 0xE7, 0x00, 0x75, 0x08, 0x95, 0x06, 0x81, 0x00,
    0xC0,
])
MOUSE_DESC = bytes([
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x05, 0x15, 0x00, 0x25, 0x01, 0x95, 0x05, 0x75, 0x01, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x03, 0x81, 0x01,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x16, 0x01, 0x80, 0x26, 0xFF, 0x7F, 0x75, 0x10, 0x95, 0x02, 0x81, 0x06,
    0x09, 0x38, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
    0x05, 0x0C, 0x0A, 0x38, 0x02, 0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,
    0xC0, 0xC0,
])

# evdev → uso HID (página 0x07). 89 (KEY_RO) é a tecla "/?" do ABNT2; 121 (KEY_KPCOMMA) o "." do keypad ABNT2.
KEYS = {
    1: 0x29, 2: 0x1E, 3: 0x1F, 4: 0x20, 5: 0x21, 6: 0x22, 7: 0x23, 8: 0x24, 9: 0x25, 10: 0x26, 11: 0x27,
    12: 0x2D, 13: 0x2E, 14: 0x2A, 15: 0x2B, 16: 0x14, 17: 0x1A, 18: 0x08, 19: 0x15, 20: 0x17, 21: 0x1C,
    22: 0x18, 23: 0x0C, 24: 0x12, 25: 0x13, 26: 0x2F, 27: 0x30, 28: 0x28, 30: 0x04, 31: 0x16, 32: 0x07,
    33: 0x09, 34: 0x0A, 35: 0x0B, 36: 0x0D, 37: 0x0E, 38: 0x0F, 39: 0x33, 40: 0x34, 41: 0x35, 43: 0x31,
    44: 0x1D, 45: 0x1B, 46: 0x06, 47: 0x19, 48: 0x05, 49: 0x11, 50: 0x10, 51: 0x36, 52: 0x37, 53: 0x38,
    55: 0x55, 57: 0x2C, 58: 0x39, 59: 0x3A, 60: 0x3B, 61: 0x3C, 62: 0x3D, 63: 0x3E, 64: 0x3F, 65: 0x40,
    66: 0x41, 67: 0x42, 68: 0x43, 69: 0x53, 70: 0x47, 71: 0x5F, 72: 0x60, 73: 0x61, 74: 0x56, 75: 0x5C,
    76: 0x5D, 77: 0x5E, 78: 0x57, 79: 0x59, 80: 0x5A, 81: 0x5B, 82: 0x62, 83: 0x63, 86: 0x64, 87: 0x44,
    88: 0x45, 89: 0x87, 96: 0x58, 98: 0x54, 99: 0x46, 102: 0x4A, 103: 0x52, 104: 0x4B, 105: 0x50,
    106: 0x4F, 107: 0x4D, 108: 0x51, 109: 0x4E, 110: 0x49, 111: 0x4C, 119: 0x48, 121: 0x85, 127: 0x65,
}
# evdev → bit do byte de modificadores
MODIFIERS = {29: 0x01, 42: 0x02, 56: 0x04, 125: 0x08, 97: 0x10, 54: 0x20, 100: 0x40, 126: 0x80}
# teclas de mídia: evdev → KEYCODE do Android (vão por INJECT_KEYCODE, não pelo teclado HID)
MEDIA = {113: 164, 114: 25, 115: 24, 163: 87, 164: 85, 165: 88, 166: 86}
# botões evdev (BTN_LEFT…BTN_EXTRA) → bit do relatório do mouse
BUTTONS = {0x110: 0x01, 0x111: 0x02, 0x112: 0x04, 0x113: 0x08, 0x114: 0x10}


def server_path() -> Path:
    exe = shutil.which("scrcpy")
    if not exe:
        raise RuntimeError("scrcpy não instalado")
    return Path(exe).resolve().parent / "scrcpy-server"


def check_version():
    out = subprocess.run(["scrcpy", "--version"], capture_output=True, text=True, timeout=10).stdout
    found = out.split()[1] if out.startswith("scrcpy ") else "?"
    if found != SERVER_VERSION:
        raise RuntimeError(f"scrcpy {found} instalado; o Nexus fala com o servidor {SERVER_VERSION}")


def uhid_create(dev_id: int, name: str, desc: bytes) -> bytes:
    raw = name.encode()[:127]
    return (struct.pack(">BHHHB", UHID_CREATE, dev_id, 0, 0, len(raw)) + raw
            + struct.pack(">H", len(desc)) + desc)


def uhid_input(dev_id: int, data: bytes) -> bytes:
    return struct.pack(">BHH", UHID_INPUT, dev_id, len(data)) + data


def keycode(action: int, code: int) -> bytes:
    return struct.pack(">BBiii", INJECT_KEYCODE, action, code, 0, 0)


def set_clipboard(seq: int, text: str) -> bytes:
    raw = text.encode()
    return struct.pack(">BqBi", SET_CLIPBOARD, seq, 0, len(raw)) + raw


_ACCEL = re.compile(r"PointerVelocityControlParameters: scale=([\d.]+), lowThreshold=([\d.]+), "
                    r"highThreshold=([\d.]+), acceleration=([\d.]+)")
_CURVE_ROW = re.compile(r"\{(inf|[\d.]+), ([\d.]+), (-?[\d.]+)\}")
_VIEWPORT = re.compile(r"Viewport INTERNAL: displayId=0,[^\n]*?orientation=(\d)[^\n]*?logicalFrame=\[0, 0, (\d+), (\d+)\]")


def _mouse_section(dump: str) -> str:
    """O trecho do `dumpsys input` do nosso mouse UHID (até o próximo dispositivo)."""
    m = re.search(r"\n  Device \d+: Nexus mouse\n", dump)  # a seção do InputReader, não a lista do EventHub
    if not m:
        return ""
    i = m.end()
    j = dump.find("\n  Device ", i)
    return dump[i:j if j > 0 else len(dump)]


def parse_accel(dump: str):
    """Aceleração do mouse no `dumpsys input` (formato de nexus/kvm.Aceleracao); None se não achar ou for absurdo.
    Android 15+ (One UI 7/8): "XScale" e "Acceleration Curve" na seção do mouse. Android ≤ 14:
    "PointerVelocityControlParameters"."""
    sec = _mouse_section(dump)
    if "Acceleration Curve" in sec:
        xs = re.search(r"XScale: ([\d.]+)", sec)
        rows = [(float("inf") if a == "inf" else float(a), float(b), float(c))
                for a, b, c in _CURVE_ROW.findall(sec[sec.index("Acceleration Curve"):])]
        xscale = float(xs.group(1)) if xs else 1.0
        if rows and 0 < xscale <= 20 and all(0 < r[1] <= 50 and r[0] > 0 for r in rows) and rows[-1][0] == float("inf"):
            enabled = "Mouse pointer acceleration enabled: false" not in dump
            return {"xscale": xscale, "enabled": enabled, "curve": rows}
    m = _ACCEL.search(sec) or _ACCEL.search(dump)
    if not m:
        return None
    scale, low, high, acc = (float(x) for x in m.groups())
    return (scale, low, high, acc) if 0 < scale <= 20 and 0 <= low < high and 1 <= acc <= 20 else None


def parse_screen(dump: str):
    """Tamanho lógico atual da tela (já com a rotação) do viewport interno; None se não achar."""
    m = _VIEWPORT.search(dump)
    if not m:
        return None
    w, h = int(m.group(2)), int(m.group(3))
    return (w, h) if 0 < w <= 20000 and 0 < h <= 20000 else None


def read_screen(serial: str, adb: str = "adb"):
    """Tamanho lógico atual da tela (com a rotação), lido agora pelo adb; None se falhar. Argumentos fixos."""
    if not _SERIAL.fullmatch(serial):
        return None
    try:
        out = subprocess.run([adb, "-s", serial, "shell", "dumpsys input | grep -m1 'Viewport INTERNAL: displayId=0'"],
                             capture_output=True, text=True, timeout=10).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    return parse_screen(out)


class HidState:
    """Estado do teclado e do mouse virtuais; cada mudança vira um relatório HID."""

    def __init__(self):
        self.mods = 0
        self.keys = []      # usos pressionados, na ordem (o relatório leva até 6)
        self.buttons = 0
        self.media = set()  # KEYCODEs de mídia apertados

    def key_report(self) -> bytes:
        keys = self.keys[-6:]
        return bytes([self.mods, 0, *keys, *([0] * (6 - len(keys)))])

    def mouse_report(self, dx=0, dy=0, wheel=0, pan=0) -> bytes:
        clamp = lambda n, lim: max(-lim, min(lim, int(n)))
        return struct.pack("<Bhhbb", self.buttons, clamp(dx, 32767), clamp(dy, 32767), clamp(wheel, 127),
                           clamp(pan, 127))

    def key(self, code: int, down: bool) -> list:
        """Mensagens de controle para a tecla evdev `code` (vazio se não houver o que mandar)."""
        if code in MEDIA:
            kc = MEDIA[code]
            if down == (kc in self.media):
                return []
            (self.media.add if down else self.media.discard)(kc)
            return [keycode(0 if down else 1, kc)]
        if code in MODIFIERS:
            bit = MODIFIERS[code]
            self.mods = self.mods | bit if down else self.mods & ~bit
        elif code in KEYS:
            usage = KEYS[code]
            if usage in self.keys:
                self.keys.remove(usage)
            if down:
                self.keys.append(usage)
        else:
            return []
        return [uhid_input(KEYBOARD_ID, self.key_report())]

    def button(self, code: int, down: bool) -> list:
        bit = BUTTONS.get(code)
        if bit is None:
            return []
        self.buttons = self.buttons | bit if down else self.buttons & ~bit
        return [uhid_input(MOUSE_ID, self.mouse_report())]

    def release_all(self) -> list:
        """Solta tudo o que está apertado (sempre manda os relatórios zerados, por garantia)."""
        out = [keycode(1, kc) for kc in sorted(self.media)]
        self.mods, self.keys, self.buttons, self.media = 0, [], 0, set()
        return out + [uhid_input(KEYBOARD_ID, self.key_report()), uhid_input(MOUSE_ID, self.mouse_report())]


def read_device_messages(recv, on_clipboard):
    """Lê as mensagens do servidor até o fim do socket. `recv(n)` devolve exatamente n bytes ou levanta."""
    while True:
        kind = recv(1)[0]
        if kind == DEV_CLIPBOARD:
            (n,) = struct.unpack(">I", recv(4))
            if n > MAX_CLIPBOARD:
                raise ValueError("clipboard grande demais")
            on_clipboard(recv(n).decode("utf-8", "replace"))
        elif kind == DEV_ACK_CLIPBOARD:
            recv(8)
        elif kind == DEV_UHID_OUTPUT:  # LEDs do teclado (Caps Lock…): ignorados
            _id, n = struct.unpack(">HH", recv(4))
            recv(n)
        else:
            raise ValueError(f"mensagem desconhecida do scrcpy-server: {kind}")


class AndroidInput:
    """Sessão de controle com um celular. `on_lost()` é chamado (de outra thread) se o adb ou o servidor
    cair: quem usa deve trazer a entrada de volta ao PC."""

    def __init__(self, serial: str, on_lost=lambda: None, adb: str = "adb", on_rotate=lambda size: None):
        if not _SERIAL.fullmatch(serial):
            raise ValueError("serial inválido")
        self.serial, self.on_lost, self.adb, self.on_rotate = serial, on_lost, adb, on_rotate
        self.hid = HidState()
        self.size = (1080, 2400)
        self.accel = (1.0, 500.0, 3000.0, 3.0)  # VelocityControl do Android (padrão AOSP); lido no start()
        self._lock = threading.Lock()
        self._sock = None
        self._proc = None
        self._port = None
        self._seq = 0
        self._clip = None
        self._clip_event = threading.Event()
        self._closed = False

    def autoclick_on(self) -> bool:
        """Leitura só: o "clique automático" da acessibilidade clica sozinho quando o ponteiro para."""
        try:
            return self._adb("shell", "settings", "get", "secure", "accessibility_autoclick_enabled",
                             timeout=10).strip() == "1"
        except (subprocess.SubprocessError, OSError):
            return False

    def _adb(self, *args, timeout=15) -> str:
        return subprocess.run([self.adb, "-s", self.serial, *args], capture_output=True, text=True,
                              timeout=timeout, check=True).stdout

    def start(self):
        check_version()
        self._adb("push", str(server_path()), REMOTE_PATH, timeout=60)
        scid = f"{secrets.randbits(31):08x}"
        self._port = int(self._adb("forward", "tcp:0", f"localabstract:scrcpy_{scid}").strip())
        cmd = (f"CLASSPATH={REMOTE_PATH} app_process / com.genymobile.scrcpy.Server {SERVER_VERSION} scid={scid} "
               "log_level=warn video=false audio=false control=true tunnel_forward=true send_device_meta=false "
               "send_dummy_byte=true cleanup=true clipboard_autosync=false")
        self._proc = subprocess.Popen([self.adb, "-s", self.serial, "shell", cmd], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self._sock = self._connect()
        with self._lock:
            self._sock.sendall(uhid_create(KEYBOARD_ID, "Nexus teclado", KEYBOARD_DESC)
                               + uhid_create(MOUSE_ID, "Nexus mouse", MOUSE_DESC))
        for _ in range(6):  # o Android leva um instante para registrar o mouse UHID (a curva fica na seção dele)
            time.sleep(0.5)
            dump = self._adb("shell", "dumpsys", "input", timeout=15)
            if "Nexus mouse" in dump:
                break
        self.accel = parse_accel(dump) or self.accel
        self.size = parse_screen(dump) or self._screen_size()
        threading.Thread(target=self._reader, name="nexus-scrcpy", daemon=True).start()
        threading.Thread(target=self._watch, name="nexus-adb", daemon=True).start()
        threading.Thread(target=self._rotation, name="nexus-rotacao", daemon=True).start()

    def _connect(self):
        # com o forward do adb, o connect funciona mesmo sem servidor: só o byte inicial prova que ele está lá
        deadline = time.monotonic() + CONNECT_SECONDS
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                break
            try:
                s = socket.create_connection(("127.0.0.1", self._port), timeout=2)
            except OSError:
                time.sleep(0.2)
                continue
            try:
                if s.recv(1) == b"\0":
                    s.settimeout(None)
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    return s
            except OSError:
                pass
            s.close()
            time.sleep(0.2)
        self.close()
        raise RuntimeError("o scrcpy-server não respondeu")

    def screen(self):
        return read_screen(self.serial, self.adb)

    def _rotation(self):
        # o scrcpy-server sem vídeo não avisa de rotação: relê o viewport a cada ROTATION_SECONDS
        while not self._closed:
            time.sleep(ROTATION_SECONDS)
            size = None if self._closed else self.screen()
            if size and size != self.size:
                self.size = size
                self.on_rotate(size)

    def _screen_size(self):
        # `wm size` dá o tamanho natural (sem rotação): só se o dumpsys não trouxer o viewport
        out = self._adb("shell", "wm", "size", timeout=10)
        sizes = re.findall(r"(\d+)x(\d+)", out)  # "Override size" (se houver) vem por último
        return (int(sizes[-1][0]), int(sizes[-1][1])) if sizes else self.size

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("scrcpy-server fechou")
            buf += chunk
        return buf

    def _got_clipboard(self, text):
        self._clip = text
        self._clip_event.set()

    def _reader(self):
        try:
            read_device_messages(self._recv, self._got_clipboard)
        except (OSError, ValueError, IndexError):
            pass
        self._lost()

    def _watch(self):
        self._proc.wait()  # o adb shell cai junto com o servidor ou com a depuração sem fio
        self._lost()

    def _lost(self):
        if not self._closed:
            self.close()
            self.on_lost()

    def _send(self, msgs):
        if not msgs or self._sock is None:
            return
        try:
            with self._lock:
                self._sock.sendall(b"".join(msgs))
        except OSError:
            self._lost()

    # API usada pela travessia
    def key(self, code, down):
        self._send(self.hid.key(code, down))

    def button(self, code, down):
        self._send(self.hid.button(code, down))

    def move(self, dx, dy):
        self._send([uhid_input(MOUSE_ID, self.hid.mouse_report(dx=dx, dy=dy))])

    def wheel(self, vertical, horizontal):
        self._send([uhid_input(MOUSE_ID, self.hid.mouse_report(wheel=vertical, pan=horizontal))])

    def release_all(self):
        self._send(self.hid.release_all())

    def set_clipboard(self, text: str):
        self._seq += 1
        self._send([set_clipboard(self._seq, text)])

    def get_clipboard(self, timeout=1.0) -> str | None:
        self._clip_event.clear()
        self._send([bytes([GET_CLIPBOARD, 0])])
        return self._clip if self._clip_event.wait(timeout) else None

    def close(self):
        """Solta tudo, destrói os dispositivos e derruba o servidor. Idempotente."""
        if self._closed:
            return
        if self._sock is not None:
            try:
                with self._lock:
                    self._sock.sendall(b"".join(self.hid.release_all()) + struct.pack(">BH", UHID_DESTROY, KEYBOARD_ID)
                                       + struct.pack(">BH", UHID_DESTROY, MOUSE_ID))
            except OSError:
                pass
        self._closed = True
        if self._sock is not None:
            self._sock.close()
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        if self._port is not None:
            subprocess.run([self.adb, "-s", self.serial, "forward", "--remove", f"tcp:{self._port}"],
                           capture_output=True, timeout=10)
