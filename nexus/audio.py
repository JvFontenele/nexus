"""Saída virtual do PipeWire para mandar o áudio do PC a um dispositivo (ADR-0004). Argumentos fixos, sem shell."""
import json
import subprocess
import time

SINK = "nexus-audio"
RATE, CHANNELS = 48000, 2
FRAME = RATE // 100 * CHANNELS * 2  # 10 ms de s16 estéreo = 1920 bytes


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=True).stdout


def node_id(name: str) -> int | None:
    # o pw-dump às vezes emite mais de um documento JSON seguido: só o primeiro (o estado atual) interessa
    objs, _ = json.JSONDecoder().raw_decode(_run(["pw-dump"]))
    for o in objs:
        if o.get("type") == "PipeWire:Interface:Node" and o.get("info", {}).get("props", {}).get("node.name") == name:
            return o["id"]
    return None


def default_sink() -> int | None:
    """id da saída padrão atual ("id 54, type …" na primeira linha do wpctl inspect)."""
    try:
        first = _run(["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"]).split(",", 1)[0]
        return int(first.split()[1])
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return None


def cleanup():
    """Remove a saída de um nexusd anterior que caiu (o nó tem object.linger e sobreviveria)."""
    try:
        nid = node_id(SINK)
        if nid is not None:
            _run(["pw-cli", "destroy", str(nid)])
    except (subprocess.SubprocessError, OSError):
        pass


def start(device_name: str) -> tuple[int, int | None]:
    """Cria a saída "Nexus → <dispositivo>" e a torna padrão. Devolve (id da saída, id da saída padrão anterior)."""
    cleanup()
    previous = default_sink()
    desc = "Nexus → " + _safe(device_name)
    _run(["pw-cli", "create-node", "adapter",
          "{ factory.name=support.null-audio-sink node.name=%s node.description=\"%s\" media.class=Audio/Sink "
          "object.linger=true audio.position=[FL FR] }" % (SINK, desc)])
    try:
        nid = node_id(SINK)
        if nid is None:
            raise RuntimeError("PipeWire não criou a saída")
        _run(["wpctl", "set-default", str(nid)])
    except Exception:
        stop_by_name(previous)  # nunca deixar o PC com uma saída padrão que ninguém escuta
        raise
    return nid, previous


def stop_by_name(previous: int | None):
    try:
        if previous is not None:
            _run(["wpctl", "set-default", str(previous)])
    except (subprocess.SubprocessError, OSError):
        pass
    cleanup()


def stop(nid: int, previous: int | None):
    try:
        if previous is not None:
            _run(["wpctl", "set-default", str(previous)])
        _run(["pw-cli", "destroy", str(nid)])
    except (subprocess.SubprocessError, OSError):
        cleanup()


def _safe(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in " -_.")[:40]  # vai dentro das propriedades do PipeWire


def playback_argv(device_name: str, key: str, target: int | None = None) -> list[str]:
    """Toca o áudio de um dispositivo (stdin) num fluxo próprio "Nexus ← …": o PipeWire mistura vários, e o volume
    de cada um fica nas Configurações de som do GNOME."""
    desc = "Nexus ← " + _safe(device_name)
    props = '{ node.name=nexus-in-%s media.name="%s" node.description="%s" }' % (_safe(key), desc, desc)
    argv = ["pw-cat", "--playback", "-P", props, "--format", "s16", "--rate", str(RATE), "--channels", str(CHANNELS),
            "--latency", "40ms"]
    return argv + (["--target", str(target)] if target is not None else []) + ["-"]


class Jitter:
    """Diz se um bloco de 10 ms que chegou entra no pw-cat. Rajada da rede ou relógios diferentes deixam o fluxo
    adiantado: acima de `max_lead` s à frente do tempo real, descarta. Se atrasou (o pw-cat tocou silêncio), recomeça a conta."""

    def __init__(self, max_lead=0.08, clock=time.monotonic):
        self.max_lead, self._clock = max_lead, clock
        self._t0, self._n = None, 0

    def admit(self) -> bool:
        now = self._clock()
        if self._t0 is None or now - self._t0 > self._n * 0.01:
            self._t0, self._n = now, 0
        if self._n * 0.01 - (now - self._t0) > self.max_lead:
            return False
        self._n += 1
        return True


def capture_argv() -> list[str]:
    return ["pw-cat", "--record", "--target", SINK, "-P", "{ stream.capture.sink=true }", "--format", "s16",
            "--rate", str(RATE), "--channels", str(CHANNELS), "--latency", "10ms", "-"]
