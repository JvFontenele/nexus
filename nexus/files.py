"""Transferência de arquivos: nomes seguros e destino no PC (a rede fica no daemon)."""
import os
from pathlib import Path

MAX_SIZE = 4 * 1024**3  # 4 GiB por arquivo
CHUNK = 256 * 1024


def safe_name(name: str) -> str:
    """Só o nome final, sem caminho, controle, separadores ou pontos no começo (nada de ../ nem .bashrc)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*')
    base = base.strip(" .")[:150]
    return base or "arquivo"


def download_dir() -> Path:
    try:
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
    except (ImportError, ValueError):
        d = None
    return Path(d or Path.home() / "Downloads") / "Nexus"


def unique_path(directory: Path, name: str) -> Path:
    """Nunca sobrescreve: "foto.jpg" → "foto (1).jpg"."""
    p = directory / name
    stem, suffix = os.path.splitext(name)
    n = 1
    while p.exists() or p.with_name(p.name + ".part").exists():
        p = directory / f"{stem} ({n}){suffix}"
        n += 1
    return p
