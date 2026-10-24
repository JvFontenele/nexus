"""trusted.json (dispositivos pareados) e os SSLContext derivados dele."""
import datetime
import json
import os
import ssl
import threading
from pathlib import Path

from .identity import fingerprint_der


class TrustStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.records: dict[str, dict] = {}
        if path.exists():
            self.records = {r["device_id"]: r for r in json.loads(path.read_text())}

    def _save(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(list(self.records.values()), f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)  # O_TRUNC num tmp antigo mantém o modo antigo
        os.replace(tmp, self.path)

    def add(self, device_id: str, name: str, kind: str, cert_pem: str):
        with self._lock:
            self.records[device_id] = {
                "device_id": device_id, "name": name, "kind": kind, "cert_pem": cert_pem,
                "paired_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "permissions": [],
            }
            self._save()

    def remove(self, device_id: str) -> bool:
        with self._lock:
            if self.records.pop(device_id, None) is None:
                return False
            self._save()
            return True

    def allow(self, device_id: str, action: str):
        with self._lock:
            perms = self.records[device_id]["permissions"]
            if action not in perms:
                perms.append(action)
                self._save()

    def deny(self, device_id: str, action: str):
        with self._lock:
            perms = self.records[device_id]["permissions"]
            if action in perms:
                perms.remove(action)
                self._save()

    def lookup_der(self, der: bytes | None) -> dict | None:
        """Registro do certificado de cliente visto no handshake (None = cliente não pareado)."""
        return None if der is None else self.records.get(fingerprint_der(der))


def server_context(cert: Path, key: Path, store: TrustStore) -> ssl.SSLContext:
    """Contexto novo a cada mudança no store; quem chama troca a referência (as conexões abertas seguem)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(cert, key)
    ctx.verify_mode = ssl.CERT_OPTIONAL
    anchors = "".join(r["cert_pem"] for r in store.records.values())
    if anchors:
        ctx.load_verify_locations(cadata=anchors)
    return ctx


def client_context(cert: Path | None = None, key: Path | None = None) -> ssl.SSLContext:
    """Cliente: não usa CA; a identidade do servidor é conferida por pin_ok após o handshake."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if cert:  # sem cert = cliente ainda não pareado
        ctx.load_cert_chain(cert, key)
    return ctx


def pin_ok(sock: ssl.SSLSocket, pinned_fp: str) -> bool:
    der = sock.getpeercert(binary_form=True)
    return der is not None and fingerprint_der(der) == pinned_fp
