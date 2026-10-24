import hashlib
import os
import ssl
import subprocess
from pathlib import Path


def fingerprint_der(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def fingerprint_pem(pem: str) -> str:
    return fingerprint_der(ssl.PEM_cert_to_DER_cert(pem))


def ensure_identity(directory: Path) -> tuple[Path, Path]:
    """Cria (uma vez) o par EC P-256 + certificado autoassinado de 10 anos. Devolve (cert, key)."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    cert, key = directory / "cert.pem", directory / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    # CN fixo: o device_id é o fingerprint do próprio certificado, então não cabe dentro dele
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-sha256", "-days", "3650", "-subj", "/CN=nexus",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )
    os.chmod(key, 0o600)
    return cert, key


def device_id(cert: Path) -> str:
    return fingerprint_pem(cert.read_text())


def check_hello_cert(device_id_: str, cert_pem: str) -> str:
    """O cert_pem do hello precisa ser um certificado válido cujo fingerprint é o device_id anunciado."""
    if cert_pem.count("-----BEGIN") != 1:
        raise ValueError("cert_pem deve conter exatamente um certificado")
    try:
        # PEM_cert_to_DER_cert só decodifica base64; load_verify_locations faz o OpenSSL parsear o X.509
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=cert_pem)
        fp = fingerprint_pem(cert_pem)
    except (ValueError, ssl.SSLError) as e:
        raise ValueError("cert_pem inválido") from e
    if fp != device_id_:
        raise ValueError("device_id não confere com o certificado")
    return fp
