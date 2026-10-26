"""Aprovar permissões do Claude Code pelo notch do Balão (hook PermissionRequest).

O hook lê o JSON do Claude Code no stdin, manda um resumo ao nexusd pelo socket de controle local e espera a decisão
clicada no notch. Qualquer erro, timeout, daemon ou notch ausente → sai sem decidir (exit 0, sem saída) e o Claude Code
pergunta no terminal como sempre. Nunca aprova por padrão; "sempre" só devolve sugestões addRules/allow que o próprio
Claude Code ofereceu. Formato: https://code.claude.com/docs/en/hooks (PermissionRequest, Permission update entries).

Roda como script (`python3 -P …/nexus/agentes.py hook`): só a stdlib e o framing do protocolo, para sair rápido.
"""
import json
import os
import socket
import sys
from pathlib import Path

if __package__ in (None, ""):  # executado como arquivo: a raiz do repositório entra no caminho de import
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nexus.proto import framing  # noqa: E402

DECISION_TIMEOUT = 110   # s que o daemon espera o clique (o hook no settings.json tem 120)
HOOK_TIMEOUT = 120
MAX_DETAIL = 4000
DECISIONS = ("allow", "deny", "always", "terminal")
DESTINATIONS = ("session", "localSettings", "projectSettings", "userSettings")
DENY_MESSAGE = "Negado pelo usuário no notch do Nexus."
# campo de tool_input que mais diz o que a ferramenta vai fazer, na ordem de preferência
DETAIL_KEYS = ("command", "file_path", "notebook_path", "url", "pattern", "path", "query", "prompt")


def _texto(v, limite: int) -> str:
    return v[:limite] if isinstance(v, str) else ""


def regras_sempre(sugestoes) -> list[dict]:
    """Só as sugestões que adicionam regras de permitir (nunca setMode, deny, diretórios…), com os tipos conferidos."""
    out = []
    for s in sugestoes if isinstance(sugestoes, list) else []:
        if not (isinstance(s, dict) and s.get("type") == "addRules" and s.get("behavior") == "allow"
                and s.get("destination") in DESTINATIONS and isinstance(s.get("rules"), list) and s["rules"]):
            continue
        rules = s["rules"]
        if all(isinstance(r, dict) and isinstance(r.get("toolName"), str) and 0 < len(r["toolName"]) <= 200
               and isinstance(r.get("ruleContent", ""), str) and len(r.get("ruleContent", "")) <= 2000
               and set(r) <= {"toolName", "ruleContent"} for r in rules):
            out.append({"type": "addRules", "rules": rules, "behavior": "allow", "destination": s["destination"]})
    return out


def rotulo_regra(r: dict) -> str:
    return f"{r['toolName']}({r['ruleContent']})" if r.get("ruleContent") else r["toolName"]


def resumir(evento: dict) -> dict:
    """O que o notch mostra: projeto, ferramenta, comando/caminho (truncado) e o que "sempre" vai liberar."""
    entrada = evento.get("tool_input") if isinstance(evento.get("tool_input"), dict) else {}
    detalhe = next((entrada[k] for k in DETAIL_KEYS if isinstance(entrada.get(k), str) and entrada[k]), None)
    if detalhe is None:
        detalhe = json.dumps(entrada, ensure_ascii=False)[:MAX_DETAIL] if entrada else ""
    cwd = _texto(evento.get("cwd"), 1000)
    return {
        "tool": _texto(evento.get("tool_name"), 100) or "?",
        "project": Path(cwd).name[:100] if cwd else "",
        "cwd": cwd,
        "detail": detalhe[:MAX_DETAIL],
        "description": _texto(entrada.get("description"), 300),
        "session": _texto(evento.get("session_id"), 8),
        "always": [rotulo_regra(r) for s in regras_sempre(evento.get("permission_suggestions")) for r in s["rules"]][:10],
    }


def validar(req: dict) -> dict:
    """Confere um pedido vindo do canal local (o hook ou qualquer processo do usuário) antes de mostrar no notch."""
    limites = {"tool": 100, "project": 100, "cwd": 1000, "detail": MAX_DETAIL, "description": 300, "session": 8}
    out = {}
    for k, n in limites.items():
        v = req.get(k, "")
        if not isinstance(v, str) or len(v) > n:
            raise ValueError(f"{k} inválido")
        out[k] = v
    always = req.get("always", [])
    if not isinstance(always, list) or len(always) > 10 or not all(isinstance(a, str) and len(a) <= 2300 for a in always):
        raise ValueError("always inválido")
    out["always"] = always
    return out


def saida(decisao: str | None, sempre: list[dict]) -> str:
    """Linha para o stdout do hook; vazia = sem decisão (o terminal pergunta)."""
    if decisao == "deny":
        d = {"behavior": "deny", "message": DENY_MESSAGE}
    elif decisao == "allow" or (decisao == "always" and sempre):
        d = {"behavior": "allow"}
        if decisao == "always":
            d["updatedPermissions"] = sempre
    else:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}})


def _socket_controle() -> Path:
    # o mesmo caminho de nexus.daemon.control_path(), sem importar o daemon inteiro
    env = os.environ.get("NEXUS_RUNTIME_DIR")
    if env:
        return Path(env) / "control.sock"
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "nexus" / "control.sock"
    data = os.environ.get("NEXUS_DATA_DIR") or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "nexus"
    return Path(data) / "run" / "nexus" / "control.sock"


def hook(entrada: str, caminho: Path | None = None, timeout: float = DECISION_TIMEOUT + 5) -> str:
    """Corpo do hook: devolve o texto para o stdout. Nunca levanta exceção."""
    try:
        evento = json.loads(entrada)
        if not isinstance(evento, dict) or evento.get("hook_event_name") != "PermissionRequest":
            return ""
        sempre = regras_sempre(evento.get("permission_suggestions"))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(caminho or _socket_controle()))
            s.settimeout(timeout)
            s.sendall(framing.encode({"cmd": "agent.request", **resumir(evento)}))
            resp = framing.read_frame(framing.recv_exact_from(s))
        decisao = resp.get("decision") if resp.get("ok") else None
        return saida(decisao if decisao in DECISIONS else None, sempre)
    except Exception:  # noqa: BLE001 — qualquer falha: sem decisão, nunca travar nem aprovar
        return ""


# ── instalação no settings.json (nunca sem o usuário ver o diff) ─────────────────────────────────────────────
def comando_hook() -> str:
    import shlex
    return f"{shlex.quote(sys.executable)} -P {shlex.quote(str(Path(__file__).resolve()))} hook"


def mesclar(config: dict, comando: str) -> dict:
    """Cópia de config com o hook PermissionRequest do Nexus; não apaga nem altera nada que já existe."""
    novo = json.loads(json.dumps(config))
    hooks = novo.setdefault("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(hooks.setdefault("PermissionRequest", []), list):
        raise ValueError("\"hooks\" do settings.json num formato inesperado; nada foi alterado")
    grupos = hooks["PermissionRequest"]
    if any(isinstance(g, dict) and any(isinstance(h, dict) and h.get("command") == comando for h in g.get("hooks", []))
           for g in grupos):
        return novo
    grupos.append({"matcher": "*", "hooks": [{"type": "command", "command": comando, "timeout": HOOK_TIMEOUT}]})
    return novo


def instalar(config_dir: Path, gravar: bool, agora=None, saida_=print) -> int:
    import datetime
    import difflib
    import shutil
    import tempfile
    alvo = (config_dir / "settings.json").resolve()  # symlink (dotfiles): grava no arquivo de verdade
    antes = alvo.read_text(encoding="utf-8") if alvo.exists() else ""
    try:
        config = json.loads(antes) if antes.strip() else {}
        if not isinstance(config, dict):
            raise ValueError("settings.json não é um objeto JSON")
        depois = json.dumps(mesclar(config, comando_hook()), indent=2, ensure_ascii=False) + "\n"
    except ValueError as e:
        saida_(f"nexus: {alvo}: {e}")
        return 1
    if json.loads(depois) == config:
        saida_(f"{alvo}: o hook do Nexus já está instalado.")
        return 0
    saida_("".join(difflib.unified_diff(antes.splitlines(True), depois.splitlines(True), str(alvo), f"{alvo} (novo)")))
    if not gravar:
        saida_("Nada foi gravado. Confira o diff e rode de novo com --yes para gravar (com backup).")
        return 0
    if alvo.exists():
        stamp = (agora or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
        backup = alvo.with_name(f"settings.json.bak-{stamp}")
        shutil.copy2(alvo, backup)
        saida_(f"Backup: {backup}")
    alvo.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=alvo.parent, prefix=".settings.json.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(depois)
    if alvo.exists():
        os.chmod(tmp, alvo.stat().st_mode & 0o777)
    os.replace(tmp, alvo)
    saida_(f"Gravado: {alvo}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["hook"]:
        try:
            texto = hook(sys.stdin.read())
        except BaseException:  # noqa: BLE001
            texto = ""
        if texto:
            print(texto)
    raise SystemExit(0)
