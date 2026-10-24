import re
import uuid


class ProtocolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


_ANY = (str, int, float, bool, list, dict, type(None))

SCHEMAS = {
    "hello":          {"device_id": str, "name": str, "kind": str, "app_version": str, "protocol": list, "cert_pem?": str},
    "pair.request":   {"commit": str},
    "pair.challenge": {"session": str, "n_s": str, "expires_in": int},
    "pair.reveal":    {"session": str, "n_c": str},
    "pair.confirm":   {"session": str, "accept": bool},
    "pair.result":    {"ok": bool, "reason?": str},
    "unpair":         {},
    "ping":           {},
    "pong":           {},
    "actions.list":   {},
    "actions.result": {"actions": list},
    "action.run":     {"action": str, "params": dict},
    "action.result":  {"ok": bool, "result?": _ANY, "error?": str},
    "share.text":     {"text": str},
    "clipboard.set":  {"text": str},
    "open.url":       {"url": str},
    "mirror.request": {},
    "file.push":      {"name": str, "size": int},
    "file.offer":     {"id": str, "name": str, "size": int},
    "file.pull":      {"id": str},
    "file.ready":     {"name?": str, "size?": int},
    "audio.attach":   {},
    "audio.start":    {},
    "audio.stop":     {},
    "audio.ready":    {"rate": int, "channels": int, "format": str},
    "error":          {"code": str, "message": str},
}

ENVELOPE_KEYS = {"v", "id", "type", "body", "re"}
MAX_STR = 65536
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX_FIELDS = {"pair.request": "commit", "pair.challenge": "n_s", "pair.reveal": "n_c", "hello": "device_id"}


def _is(value, t) -> bool:
    types = t if isinstance(t, tuple) else (t,)
    # bool é subclasse de int: só aceita bool quando o schema pede bool
    if isinstance(value, bool) and bool not in types:
        return False
    return isinstance(value, types)


def _short_str(value) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64


def _check_size(value):
    """Nenhuma string (em qualquer profundidade) pode passar de MAX_STR."""
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            if len(v) > MAX_STR:
                raise ProtocolError("too_large", "string longa demais")
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)


def validate(msg: dict) -> dict:
    if not isinstance(msg, dict):
        raise ProtocolError("bad_envelope", "mensagem não é objeto")
    extra = set(msg) - ENVELOPE_KEYS
    if extra or not {"v", "id", "type", "body"} <= set(msg):
        raise ProtocolError("bad_envelope", "chaves do envelope inválidas")
    if type(msg["v"]) is not int or msg["v"] != 1:
        raise ProtocolError("bad_version", "v deve ser 1")
    if not _short_str(msg["id"]):
        raise ProtocolError("bad_envelope", "id inválido")
    if "re" in msg and not _short_str(msg["re"]):
        raise ProtocolError("bad_envelope", "re inválido")
    type_ = msg["type"]
    if not isinstance(type_, str) or type_ not in SCHEMAS:
        raise ProtocolError("unknown_type", "tipo desconhecido")
    body = msg["body"]
    if not isinstance(body, dict):
        raise ProtocolError("bad_body", "body não é objeto")
    _check_size(body)

    schema = SCHEMAS[type_]
    fields = {k.rstrip("?"): (t, k.endswith("?")) for k, t in schema.items()}
    if set(body) - set(fields):
        raise ProtocolError("bad_body", "chave inesperada no body")
    for name, (t, optional) in fields.items():
        if name not in body:
            if not optional:
                raise ProtocolError("bad_body", f"falta {name}")
        elif not _is(body[name], t):
            raise ProtocolError("bad_body", f"tipo errado em {name}")

    hex_field = HEX_FIELDS.get(type_)
    if hex_field and not HEX64.fullmatch(body[hex_field]):
        raise ProtocolError("bad_body", f"{hex_field} deve ter 64 hex minúsculos")
    if type_ == "hello" and body["kind"] not in ("phone", "tablet", "pc"):
        raise ProtocolError("bad_body", "kind inválido")
    if type_ == "action.run":
        for k, v in body["params"].items():
            if not isinstance(k, str) or not isinstance(v, (str, int, bool)):
                raise ProtocolError("bad_body", "params só aceita str/int/bool")
    return msg


def make(type_: str, body: dict, re: str | None = None) -> dict:
    msg = {"v": 1, "id": str(uuid.uuid4()), "type": type_, "body": body}
    if re is not None:
        msg["re"] = re
    return validate(msg)
