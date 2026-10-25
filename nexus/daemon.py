"""nexusd: servidor do protocolo v1 (docs/protocol/v1.md) e canal de controle local da CLI."""
import asyncio
import collections
import ipaddress
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import nexus
from nexus import catalog, mdns
from nexus.log import log_event
from nexus.proto import framing
from nexus.proto.identity import check_hello_cert, device_id, ensure_identity
from nexus.proto.messages import ProtocolError, make, validate
from nexus.proto.pairing import PairingError, ServerPairing
from nexus.proto.trust import TrustStore, server_context

PORT = 47210
IDLE = 90
HANDSHAKE_TIMEOUT = 10
MAX_CONNS = 8
MAX_FAILURES = 5
RATE = 50        # mensagens/s por conexão
PAIR_RATE = 3    # pair.request por minuto por IP
SEEN_IDS = 1024  # ids lembrados por conexão para detectar replay


def data_dir() -> Path:
    env = os.environ.get("NEXUS_DATA_DIR")
    if env:
        return Path(env)
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "nexus"


def control_path() -> Path:
    env = os.environ.get("NEXUS_RUNTIME_DIR")
    base = Path(env) if env else Path(os.environ.get("XDG_RUNTIME_DIR") or data_dir() / "run") / "nexus"
    return base / "control.sock"


def default_listen() -> list[str]:
    """IPv4 privados (LAN) e CGNAT (tailnet), sem loopback nem bridges de contêiner/VM."""
    out = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, check=True).stdout
    skip = ("lo", "docker", "br-", "veth", "virbr")
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    addrs = []
    for iface in json.loads(out):
        if iface["ifname"].startswith(skip):
            continue
        for a in iface.get("addr_info", []):
            ip = ipaddress.ip_address(a["local"])
            if ip.is_private or ip in cgnat:
                addrs.append(str(ip))
    return addrs


class _Close(Exception):
    """Encerra a conexão sem resposta."""


class Conn:
    def __init__(self, reader, writer, ip, record):
        self.reader, self.writer, self.ip = reader, writer, ip
        self.record = record  # None = não pareado
        self.hello = None
        self.fp_c = None
        self.pairing = None
        self.failures = 0
        self._seen = collections.deque(maxlen=SEEN_IDS)
        self._seen_set = set()
        self._times = collections.deque()

    @property
    def device_id(self):
        return self.record["device_id"] if self.record else None

    def remember(self, msg_id: str) -> bool:
        if msg_id in self._seen_set:
            return False
        if len(self._seen) == SEEN_IDS:
            self._seen_set.discard(self._seen[0])
        self._seen.append(msg_id)
        self._seen_set.add(msg_id)
        return True

    def rate_ok(self, now: float) -> bool:
        while self._times and now - self._times[0] > 1:
            self._times.popleft()
        self._times.append(now)
        return len(self._times) <= RATE

    async def send(self, msg: dict):
        self.writer.write(framing.encode(msg))
        await self.writer.drain()

    def close(self):
        self.writer.close()


class Daemon:
    def __init__(self, data: Path, listen: list[str], port: int = PORT, clock=time.monotonic):
        self.cert, self.key = ensure_identity(data / "identity")
        self.fp_s = device_id(self.cert)
        self.store = TrustStore(data / "trusted.json")
        self.ctx = server_context(self.cert, self.key, self.store)
        self.listen, self.port, self.clock = listen, port, clock
        self.name = socket.gethostname()
        self.conns: set[Conn] = set()
        self.by_device: dict[str, Conn] = {}
        self.pending: dict[str, Conn] = {}
        self.pair_hits: dict[str, collections.deque] = {}
        self.audio_conn: Conn | None = None
        self.offers: dict[str, tuple[str, Path, float]] = {}  # id → (device_id, arquivo, prazo)
        self.transfers: dict[str, dict] = {}  # id → {"conn", "last"}: para progresso e cancelamento
        self.handshaking = 0
        self.servers = []
        # trocados pela ponte D-Bus (nexus/dbus.py); sem ela o daemon funciona igual
        self.emit = lambda event: None
        self.shell_present = lambda: False

    # ── ciclo de vida ────────────────────────────────────────────────────
    async def start(self):
        for host in self.listen:
            self.servers.append(await asyncio.start_server(self._handle, host, self.port))

    @property
    def bound_port(self) -> int:
        return self.servers[0].sockets[0].getsockname()[1]

    async def start_control(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        self.servers.append(await asyncio.start_unix_server(self._control, path))
        os.chmod(path, 0o600)

    async def close(self):
        for s in self.servers:
            s.close()
        for c in list(self.conns):
            c.close()
        for s in self.servers:
            await s.wait_closed()

    def reload_trust(self):
        # contexto novo para os próximos handshakes; as conexões abertas são reconferidas no store
        self.ctx = server_context(self.cert, self.key, self.store)

    # ── conexões ─────────────────────────────────────────────────────────
    async def _handle(self, reader, writer):
        ip = writer.get_extra_info("peername")[0]
        if len(self.conns) + self.handshaking >= MAX_CONNS:
            writer.close()
            return
        self.handshaking += 1
        try:
            await asyncio.wait_for(writer.start_tls(self.ctx), HANDSHAKE_TIMEOUT)
        except (ssl.SSLError, OSError, asyncio.TimeoutError):
            writer.close()
            return
        finally:
            self.handshaking -= 1
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        conn = Conn(reader, writer, ip, self.store.lookup_der(der))
        self.conns.add(conn)
        try:
            await self._loop(conn)
        except (_Close, asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, ssl.SSLError):
            pass
        finally:
            self.conns.discard(conn)
            if self.by_device.get(conn.device_id) is conn:
                del self.by_device[conn.device_id]
                self.emit({"event": "device.disconnected", "device_id": conn.device_id})
            self._drop_pending(conn)
            conn.close()

    async def _loop(self, conn: Conn):
        while True:
            header = await asyncio.wait_for(conn.reader.readexactly(4), IDLE)
            try:
                n = framing.frame_length(header)
            except framing.FrameError:
                return  # tamanho inválido ou > 1 MiB: fecha sem ler o payload
            payload = await asyncio.wait_for(conn.reader.readexactly(n), IDLE)
            if not conn.rate_ok(self.clock()):
                return
            rid = None
            try:
                raw = framing.decode_payload(payload)
                if isinstance(raw.get("id"), str) and 1 <= len(raw["id"]) <= 64:
                    rid = raw["id"]
                msg = validate(raw)
                if not conn.remember(msg["id"]):
                    raise ProtocolError("replay", "id repetido")
                await self._dispatch(conn, msg)
            except (framing.FrameError, ProtocolError) as e:
                code = getattr(e, "code", "bad_frame")
                if code != "unknown_type":  # tipo desconhecido não derruba (compatibilidade futura)
                    conn.failures += 1
                await conn.send(make("error", {"code": code, "message": getattr(e, "message", str(e))[:200]}, re=rid))
                if conn.failures >= MAX_FAILURES:
                    return

    async def _dispatch(self, conn: Conn, msg: dict):
        t, body, rid = msg["type"], msg["body"], msg["id"]
        if conn.record is not None and conn.device_id not in self.store.records:
            raise _Close()  # revogado depois do handshake
        if t == "hello":
            return await self._hello(conn, body, rid)
        if t in ("file.push", "file.pull") and conn.hello is None:
            await (self._file_push if t == "file.push" else self._file_pull)(conn, body, rid)
            raise _Close()  # conexão só de dados, como a de áudio
        if t == "audio.attach" and conn.hello is None:
            await self._audio(conn, rid)  # conexão só de áudio: não substitui a de controle
            raise _Close()
        if conn.hello is None:
            raise ProtocolError("no_hello", "envie hello primeiro")
        if t.startswith("pair."):
            return await self._pair(conn, t, body, rid)
        if conn.record is None:
            raise ProtocolError("unauthorized", "dispositivo não pareado")
        if t == "ping":
            await conn.send(make("pong", {}, re=rid))
        elif t == "unpair":
            self.revoke(conn.device_id)
            raise _Close()
        elif t == "actions.list":
            perms = self.store.records[conn.device_id]["permissions"]
            items = [{**x, "allowed": x["id"] in perms or "*phone_safe" in perms} for x in _phone_actions()]
            await conn.send(make("actions.result", {"actions": items}, re=rid))
        elif t == "action.run":
            await conn.send(make("action.result", await self._run_action(conn, body), re=rid))
        elif t == "mirror.request":
            # o próprio celular pede para ser espelhado no PC (bloco rápido); só mostra a tela dele aqui
            from nexus.actions.desktop import mirror
            out = await asyncio.to_thread(mirror, conn.record["name"], conn.ip)
            await conn.send(make("action.result", out, re=rid))
        elif t == "share.text":
            await conn.send(make("action.result", await asyncio.to_thread(self._share_text, conn, body["text"]), re=rid))
        else:
            raise ProtocolError("unsupported", f"{t} não suportado pelo servidor")

    async def _hello(self, conn: Conn, body: dict, rid: str):
        if conn.hello is not None:
            raise ProtocolError("bad_state", "hello repetido")
        if 1 not in body["protocol"]:
            raise ProtocolError("bad_version", "protocolo 1 não suportado pelo cliente")
        if conn.record:
            if body["device_id"] != conn.device_id:
                raise _Close()
        else:
            if "cert_pem" not in body:
                raise ProtocolError("bad_body", "cert_pem obrigatório antes do pareamento")
            try:
                conn.fp_c = check_hello_cert(body["device_id"], body["cert_pem"])
            except ValueError as e:
                raise ProtocolError("bad_body", str(e))
        conn.hello = body
        if conn.record:
            old = self.by_device.get(conn.device_id)
            if old:
                old.close()  # 1 conexão de controle por dispositivo: a nova substitui
            self.by_device[conn.device_id] = conn
            self.emit({"event": "device.connected", "device_id": conn.device_id, "name": conn.record["name"]})
        await conn.send(make("hello", {"device_id": self.fp_s, "name": self.name, "kind": "pc",
                                       "app_version": nexus.__version__, "protocol": [1]}, re=rid))

    # ── pareamento ───────────────────────────────────────────────────────
    async def _pair(self, conn: Conn, t: str, body: dict, rid: str):
        if conn.record is not None:
            raise ProtocolError("bad_state", "já pareado")
        if t == "pair.request":
            if conn.pairing is not None:
                raise ProtocolError("bad_state", "um pareamento por conexão")
            if conn.fp_c in self.store.records:
                raise ProtocolError("already_paired", "revogue no PC antes de parear de novo")
            now = self.clock()
            if any(c.ip == conn.ip and c.pairing and c.pairing.state in ("challenged", "revealed")
                   and c.pairing.deadline > now for c in self.conns):
                raise ProtocolError("busy", "já há pareamento pendente deste IP")
            hits = self.pair_hits.setdefault(conn.ip, collections.deque())
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= PAIR_RATE:
                raise ProtocolError("rate_limited", "muitos pedidos de pareamento")
            hits.append(now)
            conn.pairing = ServerPairing(self.fp_s, conn.fp_c, clock=self.clock)
            await conn.send(make("pair.challenge", conn.pairing.on_request(body["commit"]), re=rid))
            return
        if t not in ("pair.reveal", "pair.confirm"):
            raise ProtocolError("bad_direction", f"{t} é enviado pelo servidor")
        if conn.pairing is None:
            raise ProtocolError("bad_state", "sem pair.request")
        try:
            if t == "pair.reveal":
                code = conn.pairing.on_reveal(body["session"], body["n_c"])
                pid = conn.pairing.session[:8]
                self.pending[pid] = conn
                h = conn.hello
                print(f"nexusd: pareamento pendente {pid}: {h['name']} ({h['kind']}) fp {conn.fp_c[:16]}… "
                      f"código {code}. Confirme com: nexus pair confirm {pid}", file=sys.stderr)
                log_event({"event": "pair.pending", "pid": pid, "device_id": conn.fp_c})
                self.emit({"event": "pair.pending", **self._pending_info(pid, conn)})
                threading.Thread(target=self._pairing_notice, args=(h["name"],), daemon=True).start()
            else:
                conn.pairing.on_client_confirm(body["session"], body["accept"])
                await self._maybe_finish(conn, rid)
        except PairingError as e:
            await self._pair_failed(conn, str(e), rid)

    def _pairing_notice(self, name: str):
        if not self.shell_present():  # com o Balão, o pedido aparece no balão
            from nexus.actions.desktop import notify_pairing
            notify_pairing(name)

    async def _maybe_finish(self, conn: Conn, rid):
        if not conn.pairing.paired:
            return
        self._drop_pending(conn)
        h = conn.hello
        self.store.add(h["device_id"], h["name"], h["kind"], h["cert_pem"])
        self.reload_trust()
        log_event({"event": "pair.ok", "device_id": h["device_id"]})
        self.emit({"event": "pair.result", "device_id": h["device_id"], "name": h["name"], "ok": True})
        await conn.send(make("pair.result", {"ok": True}, re=rid))

    async def _pair_failed(self, conn: Conn, reason: str, rid):
        self._drop_pending(conn)
        conn.failures += 1
        log_event({"event": "pair.failed", "device_id": conn.fp_c, "reason": reason})
        self.emit({"event": "pair.result", "device_id": conn.fp_c, "ok": False, "reason": reason})
        await conn.send(make("pair.result", {"ok": False, "reason": reason}, re=rid))

    def _drop_pending(self, conn: Conn):
        for pid in [p for p, c in self.pending.items() if c is conn]:
            del self.pending[pid]

    def _pending_info(self, pid: str, c: Conn) -> dict:
        return {"pid": pid, "name": c.hello["name"], "kind": c.hello["kind"], "fingerprint": c.fp_c,
                "code": c.pairing.code, "expires_in": int(c.pairing.deadline - self.clock())}

    def list_pending(self) -> list[dict]:
        now = self.clock()
        return [self._pending_info(pid, c) for pid, c in self.pending.items()
                if c.pairing.state == "revealed" and c.pairing.deadline > now]

    async def confirm(self, pid: str, accept: bool) -> dict:
        conn = self.pending.get(pid)
        if conn is None:
            return {"ok": False, "error": "pareamento não encontrado ou expirado"}
        try:
            conn.pairing.on_local_confirm(accept)
        except PairingError as e:
            await self._pair_failed(conn, str(e), None)
            return {"ok": False, "error": str(e)}
        await self._maybe_finish(conn, None)
        return {"ok": True, "paired": conn.pairing.paired}

    # ── dispositivos e ações ─────────────────────────────────────────────
    def revoke(self, device_id_: str) -> bool:
        ok = self.store.remove(device_id_)
        self.reload_trust()
        for c in [c for c in self.conns if c.device_id == device_id_]:
            c.close()
        log_event({"event": "revoke", "device_id": device_id_, "ok": ok})
        return ok

    def allow(self, device_id_: str, action: str) -> dict:
        if device_id_ not in self.store.records:
            return {"ok": False, "error": "dispositivo não pareado"}
        if action != "*phone_safe":
            a = catalog.REGISTRY.get(action)
            if a is None or not a.phone_safe:
                return {"ok": False, "error": "ação inexistente ou não phone_safe"}
        self.store.allow(device_id_, action)
        return {"ok": True}

    async def _run_action(self, conn: Conn, body: dict) -> dict:
        perms = self.store.records[conn.device_id]["permissions"]
        a = catalog.REGISTRY.get(body["action"])
        allowed = (a is not None and a.phone_safe and (a.id in perms or "*phone_safe" in perms)
                   # shortcut: params de caminho não são aceitos remotamente na v1, reavaliar com transferência de arquivos
                   and not any(p.type == "path" for p in a.params)
                   and not any("=" in k for k in body["params"]))
        if not allowed:
            out = {"ok": False, "error": "forbidden"}
        else:
            pairs = [f"{k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in body["params"].items()]
            try:
                params = catalog.parse_params(a, pairs)
                result = await asyncio.to_thread(catalog.run, a.id, params)
                out = {"ok": True, "result": json.loads(json.dumps(result, default=str))}
            except Exception as e:
                out = {"ok": False, "error": type(e).__name__}
        log_event({"event": "remote.action", "device_id": conn.device_id, "action": body["action"],
                   "ok": out["ok"], "params": sorted(body["params"])})
        try:
            make("action.result", out)
        except ProtocolError:
            out = {"ok": False, "error": "result_too_large"}
        return out

    def _share_text(self, conn: Conn, text: str) -> dict:
        """Compartilhamento explícito do telefone: copia para o clipboard (wl-copy) e avisa; sem wl-copy, só avisa."""
        from nexus.actions.desktop import copy_to_clipboard, notify
        name = self.store.records[conn.device_id]["name"]
        if self.shell_present():
            # o Balão copia (St.Clipboard), registra no histórico e avisa
            self.emit({"event": "text.received", "device_id": conn.device_id, "name": name, "text": text})
            copied = True
        else:
            copied = copy_to_clipboard(text)
            try:
                notify(f"Texto de {name}" + (" (copiado)" if copied else ""), text)
            except Exception:
                pass
        log_event({"event": "share.text", "device_id": conn.device_id, "copied": copied, "chars": len(text)})
        return {"ok": True, "result": "copiado" if copied else "notificado"}

    async def _push(self, cmd: str, req: dict) -> dict:
        """PC → dispositivo, pela conexão persistente que o próprio dispositivo abriu."""
        conn = self.by_device.get(str(req.get("device_id")))
        if conn is None:
            return {"ok": False, "error": "dispositivo desconectado"}
        if cmd == "send.text":
            msg = ("share.text", {"text": str(req.get("text", ""))})
        else:
            url = str(req.get("url", ""))
            if not url.lower().startswith(("https://", "http://")):
                return {"ok": False, "error": "só http(s)"}
            msg = ("open.url", {"url": url})
        try:
            await conn.send(make(*msg))
        except ProtocolError as e:
            return {"ok": False, "error": e.code}
        except (ConnectionError, OSError):
            return {"ok": False, "error": "falha ao enviar"}
        log_event({"event": cmd, "device_id": conn.device_id})
        return {"ok": True}

    async def _audio(self, conn: Conn, rid: str):
        """Manda o áudio do PC (saída virtual "Nexus → …") como frames binários até a conexão cair."""
        if conn.record is None:
            raise ProtocolError("unauthorized", "dispositivo não pareado")
        if self.audio_conn is not None:
            self.audio_conn.close()  # um fluxo de áudio por vez: o novo assume
        self.audio_conn = conn
        from nexus import audio
        try:
            nid, previous = await asyncio.to_thread(audio.start, conn.record["name"])
        except Exception as e:
            self.audio_conn = None
            raise ProtocolError("audio_failed", f"PipeWire: {type(e).__name__}")
        proc = None
        log_event({"event": "audio.start", "device_id": conn.device_id})
        self.emit({"event": "audio.changed", "device_id": conn.device_id, "on": True})
        try:
            proc = await asyncio.create_subprocess_exec(*audio.capture_argv(), stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL)
            await conn.send(make("audio.ready", {"rate": audio.RATE, "channels": audio.CHANNELS, "format": "s16le"}, re=rid))
            # write/drain não falham num transporte já fechado: sem este teste o loop nunca terminaria
            while not conn.writer.is_closing():
                chunk = await proc.stdout.readexactly(audio.FRAME)
                conn.writer.write(struct.pack(">I", len(chunk)) + chunk)
                await asyncio.wait_for(conn.writer.drain(), 5)  # celular parado: derruba em vez de acumular atraso
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            await asyncio.to_thread(audio.stop, nid, previous)
            if self.audio_conn is conn:
                self.audio_conn = None
            log_event({"event": "audio.stop", "device_id": conn.device_id})
            self.emit({"event": "audio.changed", "device_id": conn.device_id, "on": False})

    def _transfer_start(self, conn: Conn) -> dict:
        t = {"id": new_offer_id()[:16], "conn": conn, "last": 0.0}
        self.transfers[t["id"]] = t
        return t

    def _transfer_progress(self, t: dict, direction: str, name: str, done: int, total: int):
        now = time.monotonic()
        if now - t["last"] >= 0.25 or done == total:  # no máximo 4 eventos por segundo
            t["last"] = now
            self.emit({"event": "file.progress", "id": t["id"], "direction": direction, "device_id": t["conn"].device_id,
                       "device_name": t["conn"].record["name"], "name": name, "done": done, "total": total})

    def _transfer_end(self, t: dict, direction: str, name: str, ok: bool):
        self.transfers.pop(t["id"], None)
        self.emit({"event": "file.done", "id": t["id"], "direction": direction, "device_id": t["conn"].device_id,
                   "name": name, "ok": ok})

    async def _file_push(self, conn: Conn, body: dict, rid: str):
        """Recebe um arquivo do dispositivo em ~/Downloads/Nexus (nome saneado, sem sobrescrever, .part até completar)."""
        from nexus import files
        if conn.record is None:
            raise ProtocolError("unauthorized", "dispositivo não pareado")
        size = body["size"]
        if not 0 <= size <= files.MAX_SIZE:
            raise ProtocolError("too_large", "arquivo grande demais")
        directory = files.download_dir()
        directory.mkdir(parents=True, exist_ok=True)
        dest = files.unique_path(directory, files.safe_name(body["name"]))
        part = dest.with_name(dest.name + ".part")
        ok = False
        t = self._transfer_start(conn)
        try:
            with open(part, "xb") as f:  # x: falha se outro envio pegou o mesmo nome no meio tempo
                await conn.send(make("file.ready", {}, re=rid))
                remaining = size
                while remaining:
                    n = framing.frame_length(await asyncio.wait_for(conn.reader.readexactly(4), IDLE))
                    if n > remaining:
                        raise ProtocolError("bad_frame", "mais dados que o anunciado")
                    f.write(await asyncio.wait_for(conn.reader.readexactly(n), IDLE))
                    remaining -= n
                    self._transfer_progress(t, "in", dest.name, size - remaining, size)
            os.replace(part, dest)
            ok = True
        finally:
            if not ok:
                part.unlink(missing_ok=True)
            self._transfer_end(t, "in", dest.name, ok)
        name = conn.record["name"]
        log_event({"event": "file.received", "device_id": conn.device_id, "bytes": size})
        self.emit({"event": "file.received", "device_id": conn.device_id, "name": name, "path": str(dest)})
        await conn.send(make("action.result", {"ok": True, "result": dest.name}, re=rid))
        from nexus.actions.desktop import notify
        try:
            await asyncio.to_thread(notify, f"Arquivo de {name}", f"{dest.name} salvo em {directory}")
        except Exception:
            pass

    async def _offer_file(self, device_id: str, path: str) -> dict:
        """PC → dispositivo: oferece o arquivo; o dispositivo busca por uma conexão de dados (file.pull)."""
        conn = self.by_device.get(device_id)
        if conn is None:
            return {"ok": False, "error": "dispositivo desconectado"}
        p = Path(path)
        if not p.is_file():
            return {"ok": False, "error": "arquivo não encontrado"}
        from nexus import files
        size = p.stat().st_size
        if size > files.MAX_SIZE:
            return {"ok": False, "error": "arquivo grande demais"}
        offer = new_offer_id()
        self.offers[offer] = (device_id, p, self.clock() + 300)
        await conn.send(make("file.offer", {"id": offer, "name": p.name, "size": size}))
        return {"ok": True}

    async def _file_pull(self, conn: Conn, body: dict, rid: str):
        from nexus import files
        offer = self.offers.get(body["id"])
        if conn.record is None or offer is None or offer[0] != conn.device_id or offer[2] < self.clock():
            # valida antes de consumir: um pedido alheio não pode "queimar" a oferta do dispositivo certo
            raise ProtocolError("unauthorized", "oferta inexistente, expirada ou de outro dispositivo")
        del self.offers[body["id"]]  # uso único
        _dev, path, _deadline = offer
        size = path.stat().st_size
        await conn.send(make("file.ready", {"name": path.name, "size": size}, re=rid))
        t = self._transfer_start(conn)
        ok = False
        try:
            sent = 0
            with open(path, "rb") as f:
                while chunk := f.read(files.CHUNK):
                    if conn.writer.is_closing():
                        return  # cancelado
                    conn.writer.write(struct.pack(">I", len(chunk)) + chunk)
                    await asyncio.wait_for(conn.writer.drain(), IDLE)
                    sent += len(chunk)
                    self._transfer_progress(t, "out", path.name, sent, size)
            log_event({"event": "file.sent", "device_id": conn.device_id, "bytes": size})
            await asyncio.wait_for(conn.reader.read(1), IDLE)  # espera o dispositivo fechar: só então o arquivo chegou todo
            ok = not conn.writer.is_closing() or sent == size
        finally:
            self._transfer_end(t, "out", path.name, ok)

    # ── controle local (CLI) ─────────────────────────────────────────────
    async def _control(self, reader, writer):
        try:
            sock = writer.get_extra_info("socket")
            _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                return
            req = framing.decode_payload(await reader.readexactly(framing.frame_length(await reader.readexactly(4))))
            writer.write(framing.encode(await self._control_cmd(req)))
            await writer.drain()
        except (framing.FrameError, asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _control_cmd(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "pair.pending":
            return {"ok": True, "pending": self.list_pending()}
        if cmd == "pair.confirm":
            return await self.confirm(str(req.get("pid")), bool(req.get("accept")))
        if cmd == "devices.list":
            return {"ok": True, "devices": [{k: r[k] for k in ("device_id", "name", "kind", "paired_at", "permissions")}
                                            for r in self.store.records.values()],
                    "connected": sorted(self.by_device),
                    "audio": self.audio_conn.device_id if self.audio_conn else None}
        if cmd == "devices.revoke":
            return {"ok": self.revoke(str(req.get("device_id")))}
        if cmd == "devices.allow":
            return self.allow(str(req.get("device_id")), str(req.get("action")))
        if cmd == "mirror":
            rec = self.store.records.get(str(req.get("device_id")))
            if rec is None:
                return {"ok": False, "error": "dispositivo não pareado"}
            from nexus.actions.desktop import mirror
            conn = self.by_device.get(rec["device_id"])
            return await asyncio.to_thread(mirror, rec["name"], conn.ip if conn else None)
        if cmd == "ui.open":
            # o notch do Balão pede: o daemon abre o app ou o seletor (o Shell não tem diálogo de arquivos)
            what, dev = req.get("what"), str(req.get("device_id", ""))
            if what == "app":
                argv = ["app"]
            elif what == "send-file" and dev in self.store.records:
                argv = ["send-file", dev]
            else:
                return {"ok": False, "error": "nada a abrir"}
            subprocess.Popen([sys.executable, "-m", "nexus", *argv], cwd=Path(nexus.__file__).parent.parent,
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True}
        if cmd == "file.cancel":
            t = self.transfers.get(str(req.get("id")))
            if t is None:
                return {"ok": False, "error": "transferência não encontrada"}
            t["conn"].close()
            return {"ok": True}
        if cmd == "send.file":
            return await self._offer_file(str(req.get("device_id")), str(req.get("path")))
        if cmd in ("audio.start", "audio.stop"):
            conn = self.by_device.get(str(req.get("device_id")))
            if cmd == "audio.stop" and self.audio_conn is not None:
                self.audio_conn.close()
                return {"ok": True}
            if conn is None:
                return {"ok": False, "error": "dispositivo desconectado"}
            await conn.send(make(cmd, {}))  # o celular abre (ou fecha) a conexão de áudio
            return {"ok": True}
        if cmd in ("send.text", "open.url"):
            return await self._push(cmd, req)
        if cmd == "devices.deny":
            dev_id = str(req.get("device_id"))
            if dev_id not in self.store.records:
                return {"ok": False, "error": "dispositivo não pareado"}
            self.store.deny(dev_id, str(req.get("action")))
            return {"ok": True}
        if cmd == "actions":
            return {"ok": True, "actions": _phone_actions()}
        if cmd == "status":
            return {"ok": True, "device_id": self.fp_s, "listen": self.listen, "port": self.port}
        return {"ok": False, "error": "comando desconhecido"}


def new_offer_id() -> str:
    import secrets
    return secrets.token_hex(16)


def _phone_actions() -> list[dict]:
    return [{"id": a.id, "description": a.description, "phone_safe": True,
             "params": [{"name": p.name, "type": p.type, "required": p.required} for p in a.params]}
            for a in catalog.all_actions() if a.phone_safe]


def control(req: dict, path: Path | None = None) -> dict:
    """Cliente do canal de controle (usado pela CLI)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(str(path or control_path()))
        s.sendall(framing.encode(req))
        return framing.read_frame(framing.recv_exact_from(s))


async def _serve(listen: list[str], port: int):
    d = Daemon(data_dir(), listen, port)
    await d.start()
    await d.start_control(control_path())
    from nexus import audio
    await asyncio.to_thread(audio.cleanup)
    bridge = None
    try:
        from nexus.dbus import Bridge
        bridge = Bridge(d, asyncio.get_running_loop())
        bridge.start()
    except Exception as e:  # sem D-Bus: só CLI; o Balão e o app não verão o daemon
        print(f"nexusd: D-Bus indisponível ({e})", file=sys.stderr)
        bridge = None
    unpublish = mdns.publish(d.name, d.bound_port, {"id": d.fp_s, "name": d.name, "v": "1"})
    print(f"nexusd: {d.fp_s[:16]}… ouvindo em {', '.join(listen)}:{d.bound_port}", file=sys.stderr)
    try:
        await asyncio.Event().wait()
    finally:
        if unpublish:
            unpublish()
        if bridge:
            bridge.stop()
        await d.close()


def serve(listen: list[str] | None = None, port: int = PORT) -> int:
    listen = listen or default_listen()
    if not listen:
        print("nexusd: nenhuma interface LAN/tailnet encontrada; use --listen", file=sys.stderr)
        return 3
    try:
        asyncio.run(_serve(listen, port))
    except KeyboardInterrupt:
        pass
    return 0
