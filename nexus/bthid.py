"""O PC como teclado + mouse Bluetooth LE (HID over GATT, HOGP) para o Android, sem adb (ADR-0006).

O BlueZ do PC vira periférico: registramos (sem root, a política do D-Bus de sistema deixa) uma aplicação
GATT com o serviço HID 0x1812, um anúncio LE "Nexus" e um Agent1 próprio. O celular vê um teclado e um
mouse Bluetooth comuns: cursor e teclado nativos do Android, sem depuração USB.

- O Device Information (0x180A, com o PnP ID) já é publicado pelo próprio BlueZ; não repetimos.
- Todas as características exigem criptografia **autenticada** (MITM): o Android só lê o Report Map depois
  do bond por comparação numérica. O agente só aceita se `confirm(nome, código)` disser sim (o usuário
  confere o mesmo código no celular); "just works", PIN e autorização de serviço são recusados.
- Os relatórios vão como notificação (PropertiesChanged em Value). O BlueZ manda a todos os centrais
  inscritos: dois aparelhos conectados recebem a mesma entrada. Por isso só UM aparelho usa Bluetooth
  (validado no nexusd).
- `Peripheral` vive no nexusd: a aplicação GATT é registrada uma vez e nunca desregistrada enquanto o
  daemon roda (reiniciá-la com o aparelho conectado quebra as notificações). O anúncio "descobrível", o
  Pairable e o Agent1 só existem numa janela de pareamento pedida pelo usuário; fora dela, só um anúncio
  de reconexão (não descobrível) enquanto o aparelho com bond está desconectado.
- Precisa de um GLib.MainLoop no contexto padrão (dbus-python + DBusGMainLoop): o Peripheral roda o dele
  numa thread. Os métodos do sink podem ser chamados de outra thread: o envio vai para o loop por
  GLib.idle_add (ordem mantida)."""
import threading

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from nexus.android_input import BUTTONS, KEYBOARD_DESC, KEYS, MEDIA, MODIFIERS, MOUSE_DESC, HidState

BLUEZ = "org.bluez"
CHAR, DESC, SERVICE = "org.bluez.GattCharacteristic1", "org.bluez.GattDescriptor1", "org.bluez.GattService1"
PROPS = "org.freedesktop.DBus.Properties"
ROOT = "/dev/nexus/bthid"
KEYBOARD_ID, MOUSE_ID, CONSUMER_ID = 1, 2, 3
PAIR_SECONDS = 120
# teclas de mídia: as mesmas da tabela MEDIA do android_input, como usos da página Consumer (0x0C)
CONSUMER = {113: 0xE2, 114: 0xEA, 115: 0xE9, 163: 0xB5, 164: 0xCD, 165: 0xB6, 166: 0xB7}
assert set(CONSUMER) == set(MEDIA)
# Consumer Control: um uso de 16 bits por relatório (0 = nada apertado)
CONSUMER_DESC = bytes([0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, CONSUMER_ID, 0x15, 0x00, 0x26, 0xFF, 0x03,
                       0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10, 0x95, 0x01, 0x81, 0x00, 0xC0])
APPEARANCE = 0x03C1  # teclado (0x03C0 HID genérico, 0x03C2 mouse)
AUTH = ["encrypt-authenticated-read"]


def uuid16(n):
    return f"0000{n:04x}-0000-1000-8000-00805f9b34fb"


def _with_id(desc, report_id):
    """Põe o Report ID logo depois de `Collection (Application)` (bytes 4–5): dois relatórios num só mapa."""
    assert desc[4:6] == b"\xa1\x01"
    return desc[:6] + bytes([0x85, report_id]) + desc[6:]


# os mesmos descritores do UHID do scrcpy (teclado 8 bytes com LEDs; mouse 5 botões, X/Y 16 bits, rodas)
REPORT_MAP = _with_id(KEYBOARD_DESC, KEYBOARD_ID) + _with_id(MOUSE_DESC, MOUSE_ID) + CONSUMER_DESC
HID_INFO = bytes([0x11, 0x01, 0x00, 0x02])  # bcdHID 1.11, sem país, "normally connectable"


class Rejected(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


class _Obj(dbus.service.Object):
    def __init__(self, bus, path, iface, props):
        super().__init__(bus, path)
        self.path, self.iface, self.props = path, iface, props

    @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return self.props if iface == self.iface else {}

    @dbus.service.signal(PROPS, signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass


class Desc(_Obj):
    def __init__(self, bus, path, char, uuid, value):
        super().__init__(bus, path, DESC, {"Characteristic": dbus.ObjectPath(char), "UUID": uuid,
                                           "Flags": dbus.Array(AUTH, signature="s")})
        self.value = value

    @dbus.service.method(DESC, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, opts):
        return dbus.ByteArray(self.value[int(opts.get("offset", 0)):])


class Char(_Obj):
    def __init__(self, bus, path, service, uuid, flags, value=b""):
        super().__init__(bus, path, CHAR, {"Service": dbus.ObjectPath(service), "UUID": uuid,
                                           "Flags": dbus.Array(flags, signature="s")})
        self.value = value

    @dbus.service.method(CHAR, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, opts):
        return dbus.ByteArray(self.value[int(opts.get("offset", 0)):])

    @dbus.service.method(CHAR, in_signature="aya{sv}")
    def WriteValue(self, value, opts):
        self.value = bytes(value)  # Control Point, Protocol Mode e LEDs: guardados e ignorados

    @dbus.service.method(CHAR)
    def StartNotify(self):
        pass

    @dbus.service.method(CHAR)
    def StopNotify(self):
        pass

    def notify(self, value):
        # sempre emite: ao reconectar um aparelho com bond, o BlueZ restaura a inscrição (CCCD) dele sem
        # chamar StartNotify; o próprio BlueZ só entrega a quem está inscrito
        self.value = value
        self.PropertiesChanged(CHAR, {"Value": dbus.ByteArray(value)}, [])


class App(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, ROOT)
        self.objs = []
        svc = ROOT + "/hid"
        self.objs.append(_Obj(bus, svc, SERVICE, {"UUID": uuid16(0x1812), "Primary": True}))

        def char(n, uuid, flags, value=b"", ref=None):
            c = Char(bus, f"{svc}/c{n}", svc, uuid16(uuid), flags, value)
            self.objs.append(c)
            if ref:  # Report Reference: (report id, tipo 1 = entrada, 2 = saída)
                self.objs.append(Desc(bus, f"{c.path}/d0", c.path, uuid16(0x2908), bytes(ref)))
            return c

        rw = ["encrypt-authenticated-read", "encrypt-authenticated-write", "write-without-response"]
        notify = ["encrypt-authenticated-read", "encrypt-authenticated-notify", "notify"]
        char(0, 0x2A4A, AUTH, HID_INFO)
        char(1, 0x2A4B, AUTH, REPORT_MAP)
        char(2, 0x2A4C, ["encrypt-authenticated-write", "write-without-response"])  # Control Point
        char(3, 0x2A4E, rw, b"\x01")  # Protocol Mode: só Report Protocol (sem boot)
        self.keyboard = char(4, 0x2A4D, notify, bytes(8), (KEYBOARD_ID, 1))
        self.mouse = char(5, 0x2A4D, notify, bytes(7), (MOUSE_ID, 1))
        char(6, 0x2A4D, rw, b"\x00", (KEYBOARD_ID, 2))  # LEDs do teclado (Caps Lock…)
        self.consumer = char(7, 0x2A4D, notify, bytes(2), (CONSUMER_ID, 1))

    @dbus.service.method("org.freedesktop.DBus.ObjectManager", out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        return {o.path: {o.iface: o.props} for o in self.objs}


class Advert(_Obj):
    def __init__(self, bus, name, discoverable, path):
        super().__init__(bus, path, "org.bluez.LEAdvertisement1", {
            "Type": "peripheral", "ServiceUUIDs": dbus.Array([uuid16(0x1812)], signature="s"),
            "LocalName": name, "Appearance": dbus.UInt16(APPEARANCE), "Discoverable": bool(discoverable)})

    @dbus.service.method("org.bluez.LEAdvertisement1")
    def Release(self):
        pass


class Agent(dbus.service.Object):
    """Só comparação numérica, sempre confirmada pelo usuário. Tudo o mais é recusado."""

    def __init__(self, bus, confirm, on_confirmed=lambda device: None):
        super().__init__(bus, ROOT + "/agent")
        self._bus, self._confirm, self._on_confirmed = bus, confirm, on_confirmed

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", async_callbacks=("ok", "err"))
    def RequestConfirmation(self, device, passkey, ok, err):
        try:
            name = str(dbus.Interface(self._bus.get_object(BLUEZ, device), PROPS).Get("org.bluez.Device1", "Alias"))
        except dbus.exceptions.DBusException:
            name = "?"
        code = f"{int(passkey):06d}"

        def ask():  # numa thread: a pergunta ao usuário não trava o loop
            yes = False
            try:
                yes = self._confirm(name, code) is True
                if yes:
                    self._on_confirmed(str(device))
            finally:
                GLib.idle_add(lambda: ok() if yes else err(Rejected("recusado pelo usuário")))
        threading.Thread(target=ask, daemon=True).start()

    @dbus.service.method("org.bluez.Agent1", in_signature="o")
    def RequestAuthorization(self, device):
        raise Rejected("pareamento sem confirmação recusado")

    @dbus.service.method("org.bluez.Agent1", in_signature="os")
    def AuthorizeService(self, device, uuid):
        raise Rejected("serviço não autorizado")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        raise Rejected("PIN não suportado")

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        raise Rejected("senha não suportada")

    @dbus.service.method("org.bluez.Agent1")
    def Cancel(self):
        pass

    @dbus.service.method("org.bluez.Agent1")
    def Release(self):
        pass


def dev_path(adapter: str, address: str) -> str:
    return f"{adapter}/dev_{address.upper().replace(':', '_')}"


class Peripheral:
    """O periférico HOGP do nexusd. Tudo o que toca o BlueZ roda na thread do loop (GLib.idle_add)."""

    def __init__(self, name="Nexus", adapter="/org/bluez/hci0"):
        self._name, self._adapter = name, adapter
        self._bus = self.app = None
        self._ready = threading.Event()
        self._error = None
        self._pair = None          # janela de pareamento: {"agent", "adv", "pairable", "timer", ...}
        self._reconnect = None     # anúncio de reconexão (não descobrível)
        self._watches = {}         # endereço → callback(conectado)

    # ── ciclo de vida ──
    def start(self, timeout=10):
        """Sobe a thread do loop e registra a aplicação GATT (uma vez só). Levanta se o BlueZ recusar."""
        if self._ready.is_set():
            if self._error:
                raise RuntimeError(self._error)
            return
        threading.Thread(target=self._run, name="nexus-bt", daemon=True).start()
        if not self._ready.wait(timeout) or self._error:
            raise RuntimeError(self._error or "o BlueZ não respondeu")

    def _run(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        try:
            self._bus = dbus.SystemBus()
            self.app = App(self._bus)
            ad = self._bus.get_object(BLUEZ, self._adapter)
            dbus.Interface(ad, "org.bluez.GattManager1").RegisterApplication(
                ROOT, {}, reply_handler=self._ready.set, error_handler=self._fail)
        except dbus.exceptions.DBusException as e:
            self._fail(e)
        GLib.MainLoop().run()

    def _fail(self, e):
        self._error = f"Bluetooth: {e}"
        self._ready.set()

    def _idle(self, fn):
        GLib.idle_add(lambda: fn() and False)

    def _iface(self, name):
        return dbus.Interface(self._bus.get_object(BLUEZ, self._adapter), name)

    # ── estado dos aparelhos ──
    def connected(self, address: str) -> bool:
        try:
            dev = self._bus.get_object(BLUEZ, dev_path(self._adapter, address))
            return bool(dbus.Interface(dev, PROPS).Get("org.bluez.Device1", "Connected"))
        except (dbus.exceptions.DBusException, AttributeError):
            return False

    def watch(self, address: str, on_change):
        """`on_change(conectado)` a cada mudança de Device1.Connected; mantém o anúncio de reconexão
        no ar só enquanto o aparelho com bond está desconectado."""
        def setup():
            path = dev_path(self._adapter, address)

            def changed(iface, props, _inv):
                if iface == "org.bluez.Device1" and "Connected" in props:
                    self._sync_reconnect(not bool(props["Connected"]))
                    on_change(bool(props["Connected"]))
            if address not in self._watches:
                self._bus.add_signal_receiver(changed, "PropertiesChanged", PROPS, BLUEZ, path)
            self._watches[address] = on_change
            self._sync_reconnect(not self.connected(address))
        self._idle(setup)

    def _sync_reconnect(self, wanted: bool):
        if wanted and self._reconnect is None and self._pair is None:
            self._reconnect = Advert(self._bus, self._name, False, ROOT + "/adv_reconexao")
            self._iface("org.bluez.LEAdvertisingManager1").RegisterAdvertisement(
                self._reconnect.path, {}, reply_handler=lambda: None, error_handler=lambda e: None)
        elif not wanted and self._reconnect is not None:
            self._unregister_adv(self._reconnect)
            self._reconnect = None

    def _unregister_adv(self, adv):
        try:
            self._iface("org.bluez.LEAdvertisingManager1").UnregisterAdvertisement(adv.path)
        except dbus.exceptions.DBusException:
            pass
        try:
            adv.remove_from_connection()
        except (LookupError, AttributeError):
            pass

    # ── janela de pareamento (pedida pelo usuário) ──
    def pair(self, confirm, on_bonded, on_end, seconds=PAIR_SECONDS):
        """Por `seconds`: Pairable, Agent1 (só comparação numérica, sempre confirmada) e anúncio descobrível.
        `on_bonded(endereço)` quando o bond confirmado termina; `on_end(motivo)` ao fechar a janela."""
        def open_():
            if self._pair is not None:
                return on_end("já há um pareamento por Bluetooth aberto")
            if self._reconnect is not None:
                self._unregister_adv(self._reconnect)
                self._reconnect = None
            props = self._iface(PROPS)
            win = {"pairable": bool(props.Get("org.bluez.Adapter1", "Pairable")), "on_end": on_end}
            self._pair = win

            def confirmed(device):
                win["device"] = device

            def bonded_check(iface, changed, _inv, path=None):
                if iface == "org.bluez.Device1" and changed.get("Paired") and path == win.get("device"):
                    addr = str(dbus.Interface(self._bus.get_object(BLUEZ, path), PROPS).Get("org.bluez.Device1", "Address"))
                    on_bonded(addr)
                    self._close_pair("pareado")
            win["receiver"] = self._bus.add_signal_receiver(bonded_check, "PropertiesChanged", PROPS, BLUEZ,
                                                            path_keyword="path")
            props.Set("org.bluez.Adapter1", "Pairable", True)  # sem isso o BlueZ recusa o bond
            win["agent"] = Agent(self._bus, confirm, confirmed)
            mgr = dbus.Interface(self._bus.get_object(BLUEZ, "/org/bluez"), "org.bluez.AgentManager1")
            mgr.RegisterAgent(win["agent"], "DisplayYesNo")
            mgr.RequestDefaultAgent(win["agent"])  # o celular inicia o bond: o agente padrão é quem responde
            win["adv"] = Advert(self._bus, self._name, True, ROOT + "/adv")
            self._iface("org.bluez.LEAdvertisingManager1").RegisterAdvertisement(
                win["adv"].path, {}, reply_handler=lambda: None,
                error_handler=lambda e: self._close_pair(f"anúncio recusado: {e}"))
            win["timer"] = GLib.timeout_add_seconds(seconds, lambda: self._close_pair("tempo esgotado") and False)
        self._idle(open_)

    def cancel_pair(self):
        self._idle(lambda: self._close_pair("cancelado"))

    def _close_pair(self, reason):
        win, self._pair = self._pair, None
        if win is None:
            return
        steps = [
            lambda: GLib.source_remove(win["timer"]) if "timer" in win and reason != "tempo esgotado" else None,
            lambda: self._unregister_adv(win["adv"]) if "adv" in win else None,
            lambda: dbus.Interface(self._bus.get_object(BLUEZ, "/org/bluez"),
                                   "org.bluez.AgentManager1").UnregisterAgent(win["agent"]) if "agent" in win else None,
            lambda: win["agent"].remove_from_connection() if "agent" in win else None,
            lambda: self._iface(PROPS).Set("org.bluez.Adapter1", "Pairable", win["pairable"]),
            lambda: win["receiver"].remove() if "receiver" in win else None,
        ]
        for step in steps:  # cada passo segue mesmo se outro falhar
            try:
                step()
            except (dbus.exceptions.DBusException, LookupError, AttributeError, ValueError):
                pass
        for address in self._watches:
            self._sync_reconnect(not self.connected(address))
        win["on_end"](reason)

    # ── relatórios (qualquer thread) ──
    def send(self, which, report):
        if self.app is not None:
            GLib.idle_add(lambda: getattr(self.app, which).notify(report) and False)


class BtHid:
    """Sink da Travessia por Bluetooth (move, key, button, wheel, release_all, close). Fechar só para de
    mandar relatórios (soltando tudo antes): o periférico continua registrado no nexusd."""
    exact = False

    def __init__(self, peripheral=None, size=None, accel=None, confirm=None):
        self.peripheral = peripheral
        self.hid = HidState()
        self.size = tuple(size) if size else (1080, 2400)
        self.accel = accel or (1.0, 500.0, 3000.0, 3.0)  # padrão AOSP, ou o salvo da última vez com adb
        self._media = None
        self._closed = False

    def start(self):
        pass

    def _send(self, which, report):
        if self.peripheral is not None and not self._closed:
            self.peripheral.send(which, report)

    def key(self, code, down):
        if code in CONSUMER:
            usage = CONSUMER[code] if down else 0
            if down or self._media == CONSUMER[code]:
                self._media = usage or None
                self._send("consumer", usage.to_bytes(2, "little"))
            return
        if code not in KEYS and code not in MODIFIERS:
            return
        self.hid.key(code, down)
        self._send("keyboard", self.hid.key_report())

    def button(self, code, down):
        if code in BUTTONS:
            self.hid.button(code, down)
            self._send("mouse", self.hid.mouse_report())

    def move(self, dx, dy):
        self._send("mouse", self.hid.mouse_report(dx=dx, dy=dy))

    def wheel(self, vertical, horizontal):
        self._send("mouse", self.hid.mouse_report(wheel=vertical, pan=horizontal))

    def release_all(self):
        self.hid.release_all()
        self._send("keyboard", self.hid.key_report())
        self._send("mouse", self.hid.mouse_report())
        if self._media:
            self._media = None
            self._send("consumer", bytes(2))

    def set_clipboard(self, text):
        pass  # sem canal de clipboard pelo HID

    def get_clipboard(self, timeout=1.0):
        return None

    def autoclick_on(self):
        return False  # sem adb não dá para ler; o aviso só aparece com adb

    def close(self):
        if not self._closed:
            self.release_all()
            self._closed = True
