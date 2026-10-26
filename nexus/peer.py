"""Lado de quem tem o teclado no PC↔PC: o nexusd vira cliente do nexusd do outro PC.

- `ClientPairing`: o mesmo pareamento SAS com commit/reveal do celular (docs/protocol/v1.md), com `kind:"pc"`.
  O PC alvo fica em `peers.json` (um TrustStore à parte) com o certificado pinado.
- `PeerSink`: a conexão de dados `input.attach` → `input.ready` e os frames binários (nexus/kvmwire.py),
  com a mesma API do AndroidInput, para a Travessia usar."""
import socket
import ssl
import threading
import time

import nexus
from nexus import kvmwire
from nexus.proto import framing
from nexus.proto.identity import device_id, fingerprint_der
from nexus.proto.messages import make, validate
from nexus.proto.pairing import commit, new_nonce, sas_code
from nexus.proto.trust import client_context, pin_ok

CONNECT_SECONDS = 10
READY_SECONDS = 120   # o PC alvo pode estar esperando o usuário aceitar o diálogo do RemoteDesktop
PING_SECONDS = 1.0
DEAD_SECONDS = 2.5    # sem nenhum frame do alvo por isso: conexão morta


def _connect(host, port, cert=None, key=None):
    raw = socket.create_connection((host, port), timeout=CONNECT_SECONDS)
    return client_context(cert, key).wrap_socket(raw)


class _Channel:
    def __init__(self, sock):
        self.sock = sock
        self._recv = framing.recv_exact_from(sock)

    def send(self, type_, body):
        self.sock.sendall(framing.encode(make(type_, body)))

    def recv(self, expect=None):
        msg = validate(framing.read_frame(self._recv))
        if msg["type"] == "error":
            raise RuntimeError(f"{msg['body']['code']}: {msg['body']['message']}")
        if expect and msg["type"] != expect:
            raise RuntimeError(f"esperado {expect}, veio {msg['type']}")
        return msg["body"]


class ClientPairing:
    """Pareamento com o PC em host:port. `start()` mostra o código; `finish(accept)` espera o resultado
    (o usuário do outro PC também confirma lá)."""

    def __init__(self, host, port, cert_path, my_name):
        self.host, self.port, self.cert_path, self.my_name = host, port, cert_path, my_name

    def start(self) -> dict:
        self.sock = _connect(self.host, self.port)
        der = self.sock.getpeercert(binary_form=True)
        self.fp_s = fingerprint_der(der)
        self.server_pem = ssl.DER_cert_to_PEM_cert(der)
        self.ch = _Channel(self.sock)
        cert_pem = self.cert_path.read_text()
        self.fp_c = device_id(self.cert_path)
        self.ch.send("hello", {"device_id": self.fp_c, "name": self.my_name[:64], "kind": "pc",
                               "app_version": nexus.__version__, "protocol": [1], "cert_pem": cert_pem})
        hello = self.ch.recv("hello")
        if hello["device_id"] != self.fp_s:
            raise RuntimeError("o hello do outro PC não bate com o certificado do TLS")
        self.name = hello["name"][:64]
        n_c = new_nonce()
        self.ch.send("pair.request", {"commit": commit(n_c, self.fp_c, self.fp_s)})
        ch = self.ch.recv("pair.challenge")
        self.session = ch["session"]
        self.ch.send("pair.reveal", {"session": self.session, "n_c": n_c})
        self.code = sas_code(self.session, self.fp_s, self.fp_c, ch["n_s"], n_c)
        self.sock.settimeout(70)  # o outro lado tem 60 s para confirmar
        return {"name": self.name, "fingerprint": self.fp_s, "code": self.code}

    def finish(self, accept: bool) -> bool:
        try:
            self.ch.send("pair.confirm", {"session": self.session, "accept": bool(accept)})
            return bool(self.ch.recv("pair.result")["ok"]) and accept
        finally:
            self.sock.close()


class PeerSink:
    """Conexão de entrada com o PC alvo. `on_lost(motivo)` e `on_home(fração)` vêm de outra thread."""
    exact = True  # o alvo sabe a posição real (ponteiro absoluto): ele decide a volta e manda HOME
    size = (1, 1)
    accel = (1.0, 1.0, 2.0, 1.0)

    def __init__(self, record, cert, key, on_lost=lambda reason: None, on_home=lambda frac: None,
                 clock=time.monotonic):
        self.record, self._cert, self._key = record, cert, key  # caminhos: `key` é o método de tecla
        self.on_lost, self.on_home, self._clock = on_lost, on_home, clock
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = False
        self._last_in = clock()
        self.sock = None

    def start(self):
        rec = self.record
        self.sock = _connect(rec["host"], rec["port"], self._cert, self._key)
        if not pin_ok(self.sock, rec["device_id"]):
            self.sock.close()
            raise RuntimeError("o certificado do outro PC mudou: pareie de novo")
        ch = _Channel(self.sock)
        self.sock.settimeout(READY_SECONDS)
        ch.send("input.attach", {})
        ch.recv("input.ready")
        self.sock.settimeout(None)
        self._recv = ch._recv
        self._last_in = self._clock()
        threading.Thread(target=self._reader, name="nexus-peer-in", daemon=True).start()
        threading.Thread(target=self._pinger, name="nexus-peer-ping", daemon=True).start()

    def _send(self, records):
        if self._closed:
            return
        try:
            with self._lock:
                for i in range(0, len(records), kvmwire.MAX_RECORDS):
                    self._seq += 1
                    self.sock.sendall(kvmwire.encode(self._seq, records[i:i + kvmwire.MAX_RECORDS]))
        except (OSError, kvmwire.WireError) as e:
            self._lost(f"conexão com o outro PC caiu ({e})")

    def _reader(self):
        inbound = kvmwire.Inbound(self._clock)
        try:
            while not self._closed:
                n = kvmwire.frame_length(self._recv(4))
                seq, records = kvmwire.decode(self._recv(n), kvmwire.TO_CONTROLLER)
                inbound.check(seq, records)
                self._last_in = self._clock()
                for r in records:
                    if r[0] == kvmwire.HOME:
                        self.on_home(r[1] / 65535)
                    elif r[0] == kvmwire.BYE:
                        raise kvmwire.WireError({1: "o outro PC mandou a entrada para casa",
                                                 2: "o outro PC não permite mais ser controlado"}.get(r[1], "erro no outro PC"))
        except (OSError, EOFError, kvmwire.WireError) as e:
            self._lost(str(e) if isinstance(e, kvmwire.WireError) else "conexão com o outro PC caiu")

    def _pinger(self):
        while not self._closed:
            time.sleep(PING_SECONDS)
            if self._clock() - self._last_in > DEAD_SECONDS:
                self._lost("o outro PC parou de responder")
                return
            self._send([(kvmwire.PING,)])

    def _lost(self, reason):
        if not self._closed:
            self.close()
            self.on_lost(reason)

    # API da Travessia
    def enter(self, wall, frac):
        self._send([(kvmwire.ENTER, kvmwire.EDGES.index(wall), round(min(max(frac, 0.0), 1.0) * 65535))])

    def move(self, dx, dy):
        recs = []
        dx, dy = int(dx), int(dy)
        while dx or dy:  # passos dentro do limite por registro
            sx = max(-kvmwire.MAX_DELTA, min(kvmwire.MAX_DELTA, dx))
            sy = max(-kvmwire.MAX_DELTA, min(kvmwire.MAX_DELTA, dy))
            recs.append((kvmwire.MOVE, sx, sy))
            dx, dy = dx - sx, dy - sy
        if recs:
            self._send(recs)

    def key(self, code, down):
        if 1 <= code <= 0x2FF:
            self._send([(kvmwire.KEY, code, int(bool(down)))])

    def button(self, code, down):
        if 0x110 <= code <= 0x117:
            self._send([(kvmwire.BUTTON, code, int(bool(down)))])

    def wheel(self, vertical, horizontal):
        clamp = lambda n: max(-kvmwire.MAX_WHEEL, min(kvmwire.MAX_WHEEL, int(n)))
        self._send([(kvmwire.WHEEL, clamp(vertical), clamp(horizontal))])

    def release_all(self):
        self._send([(kvmwire.LEAVE,)])  # o alvo solta tudo o que este PC apertou e sai do foco

    def set_clipboard(self, text):
        pass  # shortcut: clipboard PC↔PC fica para depois (o clipboard do PC só é lido pelo Balão)

    def get_clipboard(self, timeout=1.0):
        return None

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            with self._lock:
                self._seq += 1
                self.sock.sendall(kvmwire.encode(self._seq, [(kvmwire.LEAVE,)]))
        except (OSError, AttributeError, kvmwire.WireError):
            pass
        if self.sock is not None:
            self.sock.close()
