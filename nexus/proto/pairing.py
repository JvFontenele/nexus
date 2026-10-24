"""Pareamento SAS com commit/reveal (docs/protocol/v1.md). Sem rede: só estado e cálculo."""
import hashlib
import hmac
import re
import secrets
import time
import uuid

EXPIRES_IN = 60
_PREFIX = b"nexus-pair-v1"
_HEX64 = re.compile(r"[0-9a-f]{64}")


class PairingError(Exception):
    pass


def _b(hex64: str) -> bytes:
    if not isinstance(hex64, str) or not _HEX64.fullmatch(hex64):
        raise PairingError("hex de 64 esperado")
    return bytes.fromhex(hex64)


def commit(n_c: str, fp_c: str, fp_s: str) -> str:
    # campos de tamanho fixo (32 bytes cada): concatenação sem ambiguidade
    return hashlib.sha256(_b(n_c) + _b(fp_c) + _b(fp_s)).hexdigest()


def sas_code(session: str, fp_s: str, fp_c: str, n_s: str, n_c: str) -> str:
    # session é o único campo variável e fica entre prefixo fixo e sufixo de tamanho fixo
    h = hashlib.sha256(_PREFIX + session.encode() + _b(fp_s) + _b(fp_c) + _b(n_s) + _b(n_c)).hexdigest()
    return f"{int(h[:8], 16) % 1_000_000:06d}"


def new_nonce() -> str:
    return secrets.token_hex(32)


class ServerPairing:
    """Uma tentativa de pareamento numa conexão. Uso único: qualquer erro a encerra."""

    def __init__(self, fp_s: str, fp_c: str, clock=time.monotonic):
        self.fp_s, self.fp_c, self._clock = fp_s, fp_c, clock
        self.state = "new"  # new → challenged → revealed → done | failed
        self.code = None
        self._client_ok = self._local_ok = None

    def _fail(self, why: str):
        self.state = "failed"
        self.code = None
        raise PairingError(why)

    def _expect(self, state: str, session: str | None = None):
        if self.state != state:
            self._fail(f"fora de ordem ({self.state})")
        if session is not None and not hmac.compare_digest(session, self.session):
            self._fail("sessão errada")
        if self._clock() > self.deadline:
            self._fail("expirado")

    def on_request(self, commit_: str) -> dict:
        if self.state != "new":
            self._fail("pedido repetido")
        self._commit = commit_
        self.session, self.n_s = str(uuid.uuid4()), new_nonce()
        self.deadline = self._clock() + EXPIRES_IN
        self.state = "challenged"
        return {"session": self.session, "n_s": self.n_s, "expires_in": EXPIRES_IN}

    def on_reveal(self, session: str, n_c: str) -> str:
        self._expect("challenged", session)
        try:
            expected = commit(n_c, self.fp_c, self.fp_s)
        except PairingError:
            self._fail("n_c inválido")
        if not hmac.compare_digest(expected, self._commit):
            self._fail("commit não confere")
        self.code = sas_code(self.session, self.fp_s, self.fp_c, self.n_s, n_c)
        self.state = "revealed"
        return self.code

    def on_client_confirm(self, session: str, accept: bool):
        self._expect("revealed", session)
        if self._client_ok is not None:
            self._fail("confirmação repetida")
        self._client_ok = accept
        self._settle()

    def on_local_confirm(self, accept: bool):
        self._expect("revealed")
        if self._local_ok is not None:
            self._fail("confirmação repetida")
        self._local_ok = accept
        self._settle()

    def _settle(self):
        if self._client_ok is False or self._local_ok is False:
            self._fail("recusado")
        if self._client_ok and self._local_ok:
            self.state = "done"
            self.code = None

    @property
    def paired(self) -> bool:
        return self.state == "done"
