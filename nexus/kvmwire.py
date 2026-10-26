"""Frames binários da travessia PC↔PC (docs/protocol/v1.md, "Entrada compartilhada entre PCs").

Depois de `input.attach` → `input.ready` numa conexão de dados mTLS, cada frame é: 4 bytes big-endian com
o tamanho, depois `versão u8 | seq u32 | n u8 | n registros`. Tudo big-endian; tamanho de 6 a MAX_FRAME.
`seq` começa em 1 em cada conexão e sobe de 1 em 1 em cada frame (duplicado, fora de ordem ou pulado = erro).
Qualquer erro (versão, tipo desconhecido, campo fora do limite, registro truncado, bytes sobrando, tipo da
direção errada, taxa acima do limite) encerra a conexão, e o receptor solta tudo."""
import struct

VERSION = 1
MAX_FRAME = 256
MAX_RECORDS = 32
MAX_DELTA = 2000          # px por registro de movimento
MAX_WHEEL = 10            # cliques por registro
FRAMES_PER_S = 250
RECORDS_PER_S = 2000
MOTION_PER_S = 60000      # soma de |dx| + |dy| por segundo
EDGES = ("left", "right", "top", "bottom")

# A → B (quem tem o teclado → quem é controlado)
MOVE, KEY, BUTTON, WHEEL, ENTER, LEAVE, PING = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07
# B → A
HOME, BYE = 0x10, 0x11
BYE_SENT_HOME, BYE_NOT_ALLOWED, BYE_ERROR = 1, 2, 3

_FIELDS = {MOVE: ">hh", KEY: ">HB", BUTTON: ">HB", WHEEL: ">bb", ENTER: ">BH", LEAVE: "", PING: "",
           HOME: ">H", BYE: ">B"}
TO_TARGET = frozenset({MOVE, KEY, BUTTON, WHEEL, ENTER, LEAVE, PING})
TO_CONTROLLER = frozenset({HOME, BYE, PING})
_HEADER = ">BIB"


class WireError(Exception):
    pass


def _check(kind, values):
    if kind == MOVE and not all(-MAX_DELTA <= v <= MAX_DELTA for v in values):
        raise WireError("movimento fora do limite")
    if kind == KEY and not (1 <= values[0] <= 0x2FF and values[1] in (0, 1)):
        raise WireError("tecla inválida")
    if kind == BUTTON and not (0x110 <= values[0] <= 0x117 and values[1] in (0, 1)):
        raise WireError("botão inválido")
    if kind == WHEEL and not all(-MAX_WHEEL <= v <= MAX_WHEEL for v in values):
        raise WireError("roda fora do limite")
    if kind == ENTER and values[0] >= len(EDGES):
        raise WireError("borda inválida")
    if kind == BYE and values[0] not in (BYE_SENT_HOME, BYE_NOT_ALLOWED, BYE_ERROR):
        raise WireError("motivo inválido")


def encode(seq: int, records: list) -> bytes:
    """Frame com o prefixo de tamanho. `records` = [(tipo, *campos)]; valida como o receptor."""
    if not 1 <= len(records) <= MAX_RECORDS or not 1 <= seq <= 0xFFFFFFFF:
        raise WireError("frame inválido")
    body = struct.pack(_HEADER, VERSION, seq, len(records))
    for kind, *values in records:
        if kind not in _FIELDS:
            raise WireError("tipo desconhecido")
        _check(kind, values)
        body += bytes([kind]) + struct.pack(_FIELDS[kind], *values)
    if len(body) > MAX_FRAME:
        raise WireError("frame grande demais")
    return struct.pack(">I", len(body)) + body


def frame_length(header: bytes) -> int:
    (n,) = struct.unpack(">I", header)
    if not 6 <= n <= MAX_FRAME:
        raise WireError("tamanho de frame inválido")
    return n


def decode(payload: bytes, allowed: frozenset) -> tuple[int, list]:
    """(seq, registros) de um frame sem o prefixo; só aceita os tipos de `allowed` (a direção)."""
    if len(payload) < 6 or len(payload) > MAX_FRAME:
        raise WireError("tamanho de frame inválido")
    version, seq, n = struct.unpack_from(_HEADER, payload)
    if version != VERSION:
        raise WireError("versão desconhecida")
    if not 1 <= n <= MAX_RECORDS or seq == 0:
        raise WireError("cabeçalho inválido")
    pos, out = 6, []
    for _ in range(n):
        if pos >= len(payload):
            raise WireError("registro truncado")
        kind = payload[pos]
        if kind not in allowed:
            raise WireError("tipo desconhecido ou da direção errada")
        fmt = _FIELDS[kind]
        size = struct.calcsize(fmt) if fmt else 0
        if pos + 1 + size > len(payload):
            raise WireError("registro truncado")
        values = struct.unpack_from(fmt, payload, pos + 1) if fmt else ()
        _check(kind, values)
        out.append((kind, *values))
        pos += 1 + size
    if pos != len(payload):
        raise WireError("bytes sobrando no frame")
    return seq, out


class Inbound:
    """Sequência e limites de taxa de uma direção de uma conexão."""

    def __init__(self, clock):
        self._clock = clock
        self.seq = 0
        self._window = []  # (instante, registros, movimento)

    def check(self, seq: int, records: list):
        if seq != self.seq + 1:
            raise WireError("seq fora de ordem, duplicado ou pulado")
        self.seq = seq
        now = self._clock()
        motion = sum(abs(r[1]) + abs(r[2]) for r in records if r[0] == MOVE)
        self._window = [w for w in self._window if now - w[0] < 1.0] + [(now, len(records), motion)]
        if (len(self._window) > FRAMES_PER_S or sum(w[1] for w in self._window) > RECORDS_PER_S
                or sum(w[2] for w in self._window) > MOTION_PER_S):
            raise WireError("taxa acima do limite")
