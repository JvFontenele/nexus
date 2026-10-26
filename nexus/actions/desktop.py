"""Ações de desktop seguras para o telefone: argumentos fixos em lista, sem shell."""
import re
import shutil
import subprocess

from nexus.catalog import Action, Param, register


def _run(argv: list[str], stdin: str | None = None):
    subprocess.run(argv, input=stdin, text=True, check=True, capture_output=True, timeout=10)


def notify(title: str, body: str):
    _run(["notify-send", "--app-name=Nexus", "--", title, body[:500]])


def copy_to_clipboard(text: str) -> bool:
    """Texto vai pelo stdin, nunca como argumento. False se não há wl-copy ou ele falhou."""
    if not shutil.which("wl-copy"):
        return False
    try:
        # wl-copy fica em segundo plano servindo o clipboard: sem pipes de saída, senão o run espera por ele
        subprocess.run(["wl-copy"], input=text, text=True, check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _players() -> list[str]:
    out = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
                          "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.ListNames"],
                         capture_output=True, text=True, check=True, timeout=5).stdout
    return [n.strip(" '[]()\n") for n in out.split(",") if "org.mpris.MediaPlayer2." in n]


def _play_pause(params: dict) -> str:
    players = _players()
    if not players:
        return "nenhum player"
    _run(["gdbus", "call", "--session", "--dest", players[0], "--object-path", "/org/mpris/MediaPlayer2",
          "--method", "org.mpris.MediaPlayer2.Player.PlayPause"])
    return players[0].removeprefix("org.mpris.MediaPlayer2.")


register(Action("pc.lock", "Bloquear a tela do PC", lambda p: _run(["loginctl", "lock-session"]), phone_safe=True))
register(Action("pc.notify", "Mostrar uma notificação no PC", lambda p: notify("Nexus", p["text"]),
                (Param("text"),), phone_safe=True))
register(Action("media.play-pause", "Tocar/pausar o player do PC", _play_pause, phone_safe=True))


def ask_audio_in(name: str, timeout: int = 60) -> str | None:
    """Notificação "quer tocar o áudio dele neste PC": "uma", "sempre" ou None (negou, fechou ou o tempo acabou).
    Bloqueia; rode numa thread."""
    try:
        out = subprocess.run(["notify-send", "--app-name=Nexus", "--urgency=critical", "--wait",
                              "--action=uma=Permitir", "--action=sempre=Sempre permitir", "--action=nao=Negar", "--",
                              "Áudio de " + name[:60], f"{name[:60]} quer tocar o áudio dele neste PC."],
                             capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    return out if out in ("uma", "sempre") else None


def notify_pairing(name: str):
    """Sem o Balão: notificação com botão que abre o app Nexus (onde o código é conferido). Bloqueia; rode numa thread."""
    import sys
    try:
        out = subprocess.run(["notify-send", "--app-name=Nexus", "--urgency=critical", "--wait",
                              "--action=abrir=Abrir o Nexus", "--", "Pedido de pareamento",
                              f"{name[:60]} quer parear com este PC. Confira o código no app."],
                             capture_output=True, text=True, timeout=120).stdout
    except (subprocess.SubprocessError, OSError):
        return
    if out.strip() == "abrir":
        subprocess.Popen([sys.executable, "-m", "nexus", "app"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_SERIAL = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def adb_serial(model: str) -> str | None:
    """Serial do adb (USB ou depuração sem fio) cujo `model:` é o nome que o celular anunciou no Nexus."""
    try:
        out = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True, timeout=10).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    adb_model = re.sub(r"[^A-Za-z0-9]", "_", model)  # o adb mostra "SM-X400" como "model:SM_X400"
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device" and f"model:{adb_model}" in parts and _SERIAL.fullmatch(parts[0]):
            return parts[0]
    return None


def adb_wifi_connect(ip: str) -> None:
    """Conecta o adb à depuração sem fio anunciada por `ip` (o IP do celular pareado no Nexus, nunca outro)."""
    from nexus.mdns import browse
    for addr, port in browse("_adb-tls-connect._tcp"):
        if addr == ip and isinstance(port, int):
            subprocess.run(["adb", "connect", f"{addr}:{port}"], capture_output=True, timeout=15)
            return


def _adb_device(model: str, ip: str | None) -> tuple[str | None, str | None]:
    """(serial, erro) do celular no adb, tentando o adb sem fio pelo IP do pareado se preciso."""
    if not shutil.which("scrcpy") or not shutil.which("adb"):
        return None, "scrcpy/adb não instalados"
    serial = adb_serial(model)
    if serial is None and ip:
        adb_wifi_connect(ip)  # porta da depuração sem fio muda a cada vez: descobre pelo mDNS
        serial = adb_serial(model)
    if serial is None:
        return None, "celular não está no adb: conecte por USB ou ative a depuração sem fio"
    return serial, None


def _scrcpy(serial: str, extra: list[str]) -> dict:
    subprocess.Popen(["scrcpy", "-s", serial, *extra, "--stay-awake"],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "result": serial}


def mirror(model: str, ip: str | None = None) -> dict:
    """Espelha o celular com o scrcpy (ferramenta externa opcional). Argumentos fixos: nada vem da rede."""
    serial, err = _adb_device(model, ip)
    if err:
        return {"ok": False, "error": err}
    return _scrcpy(serial, ["--window-title", f"Nexus · {model[:40]}"])


def control(model: str, ip: str | None = None) -> dict:
    """Teclado e mouse do PC no celular, sem vídeo: a janelinha do scrcpy captura o mouse (Alt solta).
    UHID: o Android vê um teclado e um mouse físicos, com o cursor dele."""
    serial, err = _adb_device(model, ip)
    if err:
        return {"ok": False, "error": err}
    return _scrcpy(serial, ["--no-video", "--no-audio", "--keyboard=uhid", "--mouse=uhid",
                            "--window-title", f"Nexus · controle · {model[:40]}"])
