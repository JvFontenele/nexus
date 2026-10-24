"""Saída virtual do PipeWire para mandar o áudio do PC a um dispositivo (ADR-0004). Argumentos fixos, sem shell."""
import json
import subprocess

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
    desc = "Nexus → " + "".join(c for c in device_name if c.isalnum() or c in " -_.")[:40]
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


def capture_argv() -> list[str]:
    return ["pw-cat", "--record", "--target", SINK, "-P", "{ stream.capture.sink=true }", "--format", "s16",
            "--rate", str(RATE), "--channels", str(CHANNELS), "--latency", "10ms", "-"]
