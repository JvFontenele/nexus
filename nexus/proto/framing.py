import json
import struct


class FrameError(Exception):
    """Raised when a frame is malformed or violates protocol constraints."""

MAX_FRAME = 1024 * 1024


def encode(msg: dict) -> bytes:
    """Serialize *msg* to bytes: 4-byte big-endian length + JSON body."""
    body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise FrameError(f"frame size {len(body)} exceeds maximum {MAX_FRAME}")
    return struct.pack(">I", len(body)) + body


def frame_length(header: bytes) -> int:
    """Valida o cabeçalho de 4 bytes antes de ler qualquer payload."""
    if len(header) < 4:
        raise FrameError("truncated header")
    length = struct.unpack(">I", header[:4])[0]
    if length == 0:
        raise FrameError("zero-size frame not allowed")
    if length > MAX_FRAME:
        raise FrameError(f"frame size {length} exceeds maximum {MAX_FRAME}")
    return length


def decode_payload(body: bytes) -> dict:
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError(f"invalid frame body: {exc}") from exc
    if not isinstance(result, dict):
        raise FrameError("frame body must be a JSON object")
    return result


def read_frame(recv_exact):
    """Read one frame from *recv_exact* (callable returning bytes).

    Returns the decoded dict. Raises FrameError on protocol violations.
    """
    length = frame_length(recv_exact(4))
    body = recv_exact(length)
    if len(body) < length:
        raise FrameError("truncated frame body")
    return decode_payload(body)


def recv_exact_from(sock):
    """Return a recv_exact(n) callable that reads exactly *n* bytes from *sock*.

    Handles partial reads by repeatedly calling sock.recv.
    """
    buf = bytearray()

    def recv_exact(n):
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("connection closed")
            buf.extend(chunk)
        result = bytes(buf[:n])
        del buf[:n]
        return result

    return recv_exact