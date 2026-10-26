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
# permissão no sentido inverso das ações: o PC pode controlar este dispositivo (padrão: desligada)
INPUT_CONTROL = "input.control"
INPUT_INJECT = "input.inject"  # este PC aceita ser controlado por aquele PC (padrão: desligada)
TRANSPORTS = ("adb", "bluetooth")
INPUT_DEAD = 2.5  # s sem frame do controlador: solta tudo e encerra
SENT_HOME = 5.0   # s em que um PC mandado para casa não pode voltar
HOST_RE = __import__("re").compile(r"[A-Za-z0-9.:-]{1,253}")
AGENT_TIMEOUT = 110  # s: pedido de permissão do Claude Code sem clique expira (o terminal pergunta)
MAX_AGENT_REQUESTS = 20
AUTOCLICK_WARNING = ("O clique automático do Android está ligado: o celular vai clicar sozinho quando o ponteiro parar. "
                     "Desligue em Acessibilidade.")


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
        self.kvm = None  # travessia ativa (este PC controla): {"device_id", "capture", "travessia"}
        self.owner = None  # posse (outro PC controla este): {"device_id", "conn", "injector", "receiver"}
        self.sent_home: dict[str, tuple[float, int]] = {}  # device_id → (prazo monotônico, sessão de posse)
        self.peers = TrustStore(data / "peers.json")  # PCs que este controla (certificado pinado)
        self.bt = None  # periférico BLE HOGP (ADR-0006), criado sob demanda
        self.bt_factory = lambda: __import__("nexus.bthid").bthid.Peripheral(self.name[:20] or "Nexus")
        self._bt_lock = threading.Lock()
        self._bt_watched: set[str] = set()
        self.bt_confirms: dict[str, dict] = {}
        self._aloop = None
        self.peer_pairings: dict[str, tuple] = {}
        self.agent_requests: dict[str, dict] = {}  # id → {"info", "fut"}: pedidos de permissão do Claude Code
        self.agent_timeout = AGENT_TIMEOUT
        self.agent_ui = False  # o notch com o cartão de aprovação já se anunciou (agent.pending)
        from nexus import inject
        tokens = inject.TokenStore(data / "remote-desktop.token")
        self.injector_factory = lambda on_lost: inject.RemoteDesktop(tokens, on_lost)

    # ── ciclo de vida ────────────────────────────────────────────────────
    async def start(self):
        self._aloop = asyncio.get_running_loop()
        if any(r.get("transport") == "bluetooth" for r in self.store.records.values()):
            asyncio.ensure_future(asyncio.to_thread(self._bt_ensure))  # o periférico vive enquanto o daemon roda
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
        await self._input_stop_for(None)
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
        if t == "input.attach" and conn.hello is None:
            await self._input_attach(conn, rid)  # conexão só de entrada de outro PC
            raise _Close()
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
        if action not in ("*phone_safe", INPUT_CONTROL, INPUT_INJECT):
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

    # ── travessia do teclado e do mouse (nexus/kvm.py) ───────────────────
    def _input_status(self) -> dict:
        k, o = self.kvm, self.owner
        mux = k.get("mux") if k else None
        return {"active": bool(k), "targets": sorted(mux.targets) if mux else [],
                "current": mux.current if mux else None, "away": bool(mux and mux.away),
                "owner": o["device_id"] if o else None}

    def _target(self, dev_id: str):
        """Registro do alvo: celular/tablet pareado (trusted.json) ou PC que este controla (peers.json)."""
        if dev_id in self.store.records:
            return self.store, self.store.records[dev_id]
        if dev_id in self.peers.records:
            return self.peers, self.peers.records[dev_id]
        return None, None

    def _arranged(self) -> list:
        """(store, registro) de todos os alvos com arranjo, permissão e "Usar na borda" ligado: entram na borda."""
        out = [(self.store, r) for r in self.store.records.values()
               if r.get("arrangement") and INPUT_CONTROL in r["permissions"] and r.get("edge_on", True)]
        return out + [(self.peers, r) for r in self.peers.records.values()
                      if r.get("arrangement") and r.get("edge_on", True)]

    def _input_config(self, req: dict) -> dict:
        dev_id = str(req.get("device_id"))
        store, rec = self._target(dev_id)
        if rec is None:
            return {"ok": False, "error": "dispositivo não pareado"}
        if "enabled" in req:  # "Usar na borda": desligado, fica de fora (com a borda ativa, sai ou entra já)
            on = req["enabled"] is True
            store.update(dev_id, "edge_on", on)
            k = self.kvm
            if k and "mux" in k:
                if not on and dev_id in k["mux"].targets:
                    self._target_lost(k, dev_id, "tirado da borda")
                elif on and (store is self.peers or INPUT_CONTROL in rec["permissions"]):
                    asyncio.ensure_future(self._target_back(k, dev_id))
            if "edge" not in req and "transport" not in req:
                return {"ok": True}
        if "transport" in req:
            t = req["transport"]
            if t not in TRANSPORTS or store is not self.store:
                return {"ok": False, "error": "transporte inválido"}
            other = next((r for r in self.store.records.values()
                          if r["device_id"] != dev_id and r.get("transport") == "bluetooth"), None)
            if t == "bluetooth" and other:  # o BlueZ manda o relatório a todos os inscritos: um aparelho só
                return {"ok": False, "error": f"só um aparelho pode usar Bluetooth; hoje é o {other['name']}"}
            store.update(dev_id, "transport", t)
            if t == "bluetooth":
                asyncio.ensure_future(asyncio.to_thread(self._bt_ensure))
            if "edge" not in req:
                return {"ok": True}
        try:
            arr = check_arrangement(req)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        for other in list(self.store.records.values()) + list(self.peers.records.values()):
            o = other.get("arrangement")
            if (other["device_id"] != dev_id and o and o["edge"] == arr["edge"] and o["monitor"] == arr["monitor"]
                    and arr["start"] < o["end"] and o["start"] < arr["end"]):
                return {"ok": False, "error": f"esse trecho da borda já é do {other['name']}"}
        store.update(dev_id, "arrangement", arr)
        return {"ok": True}

    async def _open_sink(self, store, rec, session, loop):
        """Abre o alvo: scrcpy-server no celular/tablet, input.attach no outro PC."""
        from nexus import android_input, peer
        from nexus.actions import desktop
        dev_id = rec["device_id"]
        lost = lambda *reason: self._target_lost(session, dev_id, reason[0] if reason else "adb/scrcpy caiu")
        if rec.get("transport") == "bluetooth":
            from nexus import bthid
            if not rec.get("bt_address"):
                raise RuntimeError("pareie por Bluetooth primeiro")
            periph = await asyncio.to_thread(self._bt_ensure)
            if not await asyncio.to_thread(periph.connected, rec["bt_address"]):
                raise RuntimeError("não está conectado por Bluetooth (entra na borda quando conectar)")
            return bthid.BtHid(periph, rec.get("screen"), accel_from(rec.get("accel")))
        if store is self.peers:
            sink = peer.PeerSink(rec, self.cert, self.key, on_lost=lost,
                                 on_home=lambda frac: session["mux"].targets.get(dev_id) and
                                 session["mux"].targets[dev_id].remote_home(frac))
        else:
            conn = self.by_device.get(dev_id)
            serial, err = await asyncio.to_thread(desktop._adb_device, rec["name"], conn.ip if conn else None)
            if err:
                raise RuntimeError(err)
            sink = android_input.AndroidInput(serial, on_lost=lost, on_rotate=lambda size: loop.call_soon_threadsafe(
                self._set_screen, dev_id, size))
        try:
            await asyncio.to_thread(sink.start)
        except Exception:
            sink.close()
            raise
        return sink

    async def _input_start(self, _dev_id=None) -> dict:
        """"Ativar borda": vale para todos os alvos arranjados e com permissão. Uma sessão InputCapture (só
        agora: o GNOME pergunta), uma barreira por alvo, um alvo ativo por vez."""
        if self.owner:  # anti-laço: um PC controlado não controla ninguém
            return {"ok": False, "error": "este PC está sendo controlado por outro agora"}
        if self.kvm:
            return {"ok": False, "error": "a borda já está ativa"}
        cands = self._arranged()
        if not cands:
            return {"ok": False, "error": "posicione um dispositivo ao lado de um monitor primeiro"}
        from nexus import capture, kvm
        loop = asyncio.get_running_loop()
        session = {}
        self.kvm = session  # reservado antes de qualquer await: dois input.start nunca passam juntos
        session["mux"] = mux = kvm.Mux(lambda p: session["capture"].release(p),
                                       on_end=lambda reason: loop.call_soon_threadsafe(self._input_ended, session, reason),
                                       on_unplaced=lambda keys: [self._target_lost(session, k, "a barreira não coube") for k in keys])
        opened, failures, warnings = [], {}, []
        for store, rec in cands:  # um de cada vez: o adb e o diálogo do outro PC não gostam de pressa
            try:
                sink = await self._open_sink(store, rec, session, loop)
            except Exception as e:  # versão do scrcpy, adb fora do ar, outro PC recusou ou não respondeu
                failures[rec["name"]] = str(e) or type(e).__name__
                continue
            opened.append((store, rec, sink))
            if store is self.peers and getattr(sink, "zones", None):
                zs = sink.zones
                self._set_screen(rec["device_id"], (max(z[0] + z[2] for z in zs) - min(z[0] for z in zs),
                                                    max(z[1] + z[3] for z in zs) - min(z[1] for z in zs)))
            elif store is self.store and rec.get("transport") != "bluetooth":
                self._set_screen(rec["device_id"], sink.size)
                store.update(rec["device_id"], "accel", sink.accel)  # para quando o aparelho vier por Bluetooth
                if await asyncio.to_thread(sink.autoclick_on):
                    warnings.append(f"{rec['name']}: {AUTOCLICK_WARNING}")
        if not opened:
            self.kvm = None
            return {"ok": False, "error": "; ".join(f"{n}: {e}" for n, e in failures.items())}
        session["regions"] = []
        for store, rec, sink in opened:
            self._add_target(session, rec, sink)
        session["capture"] = capture.Capture(list(session["regions"]), mux)
        session["capture"].start()
        log_event({"event": "input.start", "targets": [r["device_id"] for _s, r, _k in opened]})
        for _s, rec, _k in opened:
            self.emit({"event": "input.crossed", "device_id": rec["device_id"], "away": False, "active": True})
        warnings += [f"{n}: {e}" for n, e in failures.items()]
        return {"ok": True, "targets": [r["device_id"] for _s, r, _k in opened],
                **({"warning": "\n".join(warnings)} if warnings else {})}

    def _add_target(self, session: dict, rec: dict, sink):
        """Põe o alvo no Mux e a barreira dele na lista (a Capture é avisada por quem chama, se já existe)."""
        from nexus import kvm
        dev_id, arr, mux = rec["device_id"], rec["arrangement"], session["mux"]

        def crossed(away, reason=None):
            self.emit({"event": "input.crossed", "device_id": dev_id, "away": away, "active": True, "reason": reason})
        mux.add(dev_id, kvm.Travessia(arr["edge"], sink, mux._release, start=arr["start"], end=arr["end"],
                                      on_change=crossed))
        session["regions"].append((dev_id, arr["edge"], (tuple(arr["monitor"]), arr["start"], arr["end"])))

    async def _target_back(self, session: dict, dev_id: str):
        """Um alvo voltou (ex.: conectou por Bluetooth) com a borda ativa: entra com a barreira dele."""
        store, rec = self._target(dev_id)
        if self.kvm is not session or rec is None or dev_id in session["mux"].targets or not rec.get("arrangement"):
            return
        try:
            sink = await self._open_sink(store, rec, session, asyncio.get_running_loop())
        except Exception:
            return
        if self.kvm is not session or dev_id in session["mux"].targets:
            sink.close()
            return
        self._add_target(session, rec, sink)
        session["capture"].set_regions(session["regions"])
        self.emit({"event": "input.crossed", "device_id": dev_id, "away": False, "active": True})

    # ── Bluetooth LE HOGP (nexus/bthid.py, ADR-0006) ─────────────────────
    def _bt_ensure(self):
        """Sobe o periférico BLE uma vez (thread GLib própria) e vigia a conexão do aparelho com bond.
        Nunca é desregistrado enquanto o daemon roda. Bloqueia: chame numa thread."""
        with self._bt_lock:
            if self.bt is None:
                periph = self.bt_factory()
                periph.start()
                self.bt = periph
            for rec in self.store.records.values():
                addr = rec.get("bt_address")
                if rec.get("transport") == "bluetooth" and addr and addr not in self._bt_watched:
                    self._bt_watched.add(addr)
                    dev_id = rec["device_id"]
                    self.bt.watch(addr, lambda on, dev_id=dev_id: self._aloop.call_soon_threadsafe(
                        self._bt_changed, dev_id, on))
            return self.bt

    def _bt_changed(self, dev_id: str, connected: bool):
        """Device1.Connected mudou: a barreira do aparelho existe só enquanto ele está conectado."""
        self.emit({"event": "bt.connected", "device_id": dev_id, "connected": connected})
        k = self.kvm
        if not k or "mux" not in k:
            return
        if not connected:
            self._target_lost(k, dev_id, "desconectou do Bluetooth")
        else:
            asyncio.ensure_future(self._target_back(k, dev_id))

    async def _bt_pair(self, dev_id: str) -> dict:
        """"Parear por Bluetooth": abre a janela (anúncio descobrível, Pairable e Agent1) por 2 min."""
        rec = self.store.records.get(dev_id)
        if rec is None or rec.get("transport") != "bluetooth":
            return {"ok": False, "error": "escolha Bluetooth para este aparelho primeiro"}
        try:
            periph = await asyncio.to_thread(self._bt_ensure)
        except Exception as e:
            return {"ok": False, "error": str(e) or type(e).__name__}
        loop = self._aloop

        def confirm(name, code):  # thread do agente: pergunta no app e espera a resposta (60 s)
            pid = new_offer_id()[:8]
            entry = {"event": threading.Event(), "ok": False}
            self.bt_confirms[pid] = entry
            loop.call_soon_threadsafe(self.emit, {"event": "bt.confirm", "pid": pid, "device_id": dev_id,
                                                  "name": name[:64], "code": code})
            entry["event"].wait(60)
            self.bt_confirms.pop(pid, None)
            return entry["ok"] is True

        def bonded(address):
            def save():
                self.store.update(dev_id, "bt_address", address)
                log_event({"event": "bt.bonded", "device_id": dev_id})
                asyncio.ensure_future(asyncio.to_thread(self._bt_ensure))  # passa a vigiar a conexão
            loop.call_soon_threadsafe(save)

        periph.pair(confirm, bonded, lambda reason: loop.call_soon_threadsafe(
            self.emit, {"event": "bt.pair.end", "device_id": dev_id, "reason": reason}))
        return {"ok": True, "seconds": 120}

    def _target_lost(self, session: dict, dev_id: str, reason: str):
        """Um alvo caiu (qualquer thread): sai só a barreira dele; sem alvos, a sessão acaba."""
        mux = session.get("mux")
        if mux is None or dev_id not in mux.targets:
            return
        left = mux.drop(dev_id)
        session["regions"] = [r for r in session.get("regions", []) if r[0] != dev_id]
        self.emit({"event": "input.crossed", "device_id": dev_id, "away": False, "active": False, "reason": reason})
        cap = session.get("capture")
        if cap is not None:
            if left:
                cap.set_regions(session["regions"])
            else:
                threading.Thread(target=cap.stop, daemon=True).start()

    def _set_screen(self, dev_id: str, size):
        """Guarda o tamanho lógico atual da tela do alvo (proporção no editor quando não houver adb)."""
        store, rec = self._target(dev_id)
        if rec is not None and size and list(size) != rec.get("screen"):
            store.update(dev_id, "screen", [int(size[0]), int(size[1])])
            self.emit({"event": "input.screen", "device_id": dev_id, "screen": [int(size[0]), int(size[1])]})

    async def _input_screen(self, dev_id: str) -> dict:
        """Lê agora, pelo adb, o tamanho da tela do celular/tablet (com a rotação) e guarda."""
        store, rec = self._target(dev_id)
        if rec is None:
            return {"ok": False, "error": "dispositivo não pareado"}
        if store is self.store:
            from nexus import android_input
            from nexus.actions import desktop
            serial = await asyncio.to_thread(desktop.adb_serial, rec["name"])
            size = serial and await asyncio.to_thread(android_input.read_screen, serial)
            if size:
                self._set_screen(dev_id, size)
        return {"ok": True, "screen": rec.get("screen")}

    def _input_ended(self, session: dict, reason):
        if self.kvm is session:
            self.kvm = None
        log_event({"event": "input.stop"})
        self.emit({"event": "input.crossed", "device_id": None, "away": False, "active": False, "reason": reason})

    async def _input_stop_for(self, dev_id: str | None):
        """Encerra a borda inteira (None) ou só tira um alvo dela. Fechar a sessão devolve a entrada ao PC."""
        k = self.kvm
        if not k or "capture" not in k:
            return
        if dev_id is None:
            await asyncio.to_thread(k["capture"].stop)
        elif dev_id in k["mux"].targets:
            await asyncio.to_thread(self._target_lost, k, dev_id, "permissão retirada")

    # ── PC↔PC, lado controlado (nexus/inject.py, kvmwire.py) ─────────────
    async def _input_attach(self, conn: Conn, rid: str):
        """Conexão de dados de outro PC que quer controlar este. Posse exclusiva decidida aqui, no loop
        do asyncio (sem await entre conferir e tomar a posse), sem confiar no portal."""
        rec = conn.record
        refuse = None
        if rec is None or rec["device_id"] not in self.store.records:
            refuse = ("unauthorized", "dispositivo não pareado")
        elif rec.get("kind") != "pc" or INPUT_INJECT not in rec["permissions"]:
            refuse = ("not_allowed", "este PC não permite ser controlado por esse dispositivo")
        elif self.kvm is not None:
            refuse = ("busy", "este PC está controlando outro agora")  # anti-laço
        elif self.clock() < self.sent_home.get(rec["device_id"], (0, 0))[0]:
            refuse = ("sent_home", "mandado para casa há pouco; tente em alguns segundos")
        elif self.owner is not None:
            refuse = ("owned", "outro PC já controla este")
        if refuse:
            await conn.send(make("error", {"code": refuse[0], "message": refuse[1]}, re=rid))
            return
        from nexus import inject, kvmwire
        loop = asyncio.get_running_loop()
        session = {"device_id": rec["device_id"], "conn": conn}
        self.owner = session
        receiver = None
        injector = self.injector_factory(lambda reason: loop.call_soon_threadsafe(conn.writer.transport.abort))
        session["injector"] = injector
        self.emit({"event": "input.owner", "device_id": rec["device_id"], "active": True})
        log_event({"event": "input.owner", "device_id": rec["device_id"], "active": True})
        try:
            try:
                await asyncio.to_thread(injector.start)
            except Exception as e:
                await conn.send(make("error", {"code": "portal", "message": (str(e) or type(e).__name__)[:200]}, re=rid))
                return
            receiver = inject.Receiver(injector, injector.zones, self.clock)
            session["receiver"] = receiver
            await conn.send(make("input.ready", {"zones": [list(z) for z in injector.zones]}, re=rid))
            await self._input_frames(conn, session, receiver, kvmwire)
        except (kvmwire.WireError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            if receiver is not None:
                receiver.release_all()  # desconexão, timeout, revogação, portal perdido, erro de parsing
            await asyncio.to_thread(injector.close)
            if self.owner is session:
                self.owner = None
            conn.writer.transport.abort()  # conexão só de dados: sem esperar o close_notify do outro lado
            self.emit({"event": "input.owner", "device_id": rec["device_id"], "active": False})
            log_event({"event": "input.owner", "device_id": rec["device_id"], "active": False})

    async def _input_frames(self, conn: Conn, session: dict, receiver, kvmwire):
        inbound = kvmwire.Inbound(self.clock)
        session["out_seq"] = 0
        last_ping = self.clock()
        while True:
            n = kvmwire.frame_length(await asyncio.wait_for(conn.reader.readexactly(4), INPUT_DEAD))
            seq, records = kvmwire.decode(await asyncio.wait_for(conn.reader.readexactly(n), INPUT_DEAD),
                                          kvmwire.TO_TARGET)
            inbound.check(seq, records)
            if session.get("ended"):
                return
            rec = self.store.records.get(session["device_id"])
            if rec is None or INPUT_INJECT not in rec["permissions"]:
                await self._bye(session, kvmwire.BYE_NOT_ALLOWED)
                return
            out = receiver.handle(records)
            if out:
                await self._send_frame(session, out)
            if self.clock() - last_ping >= 1.0:
                last_ping = self.clock()
                await self._send_frame(session, [(kvmwire.PING,)])

    async def _send_frame(self, session: dict, records):
        from nexus import kvmwire
        session["out_seq"] = session.get("out_seq", 0) + 1
        w = session["conn"].writer
        w.write(kvmwire.encode(session["out_seq"], records))
        await w.drain()

    async def _bye(self, session: dict, reason: int):
        """Encerra a posse: solta tudo já, avisa o controlador (BYE) e fecha a conexão."""
        session["ended"] = True
        if session.get("receiver"):
            session["receiver"].release_all()
            session["receiver"].focus = False
        try:
            from nexus import kvmwire
            await self._send_frame(session, [(kvmwire.BYE, reason)])
        except (ConnectionError, OSError):
            pass
        session["conn"].writer.transport.abort()

    def _send_home(self, dev_id: str | None = None):
        """Manda o dono atual para casa e recusa a volta dele por SENT_HOME s (cancelável pelo devices.allow)."""
        from nexus import kvmwire
        o = self.owner
        if o and (dev_id is None or o["device_id"] == dev_id):
            self.sent_home[o["device_id"]] = (self.clock() + SENT_HOME, id(o))  # timer monotônico da sessão de posse
            asyncio.ensure_future(self._bye(o, kvmwire.BYE_SENT_HOME))

    def _cancel_sent_home(self, dev_id: str):
        self.sent_home.pop(dev_id, None)

    # ── PC↔PC, lado de quem controla (nexus/peer.py) ─────────────────────
    async def _peer_pair(self, req: dict) -> dict:
        from nexus import peer
        host, port = req.get("host"), req.get("port", PORT)
        if not isinstance(host, str) or not HOST_RE.fullmatch(host) or type(port) is not int or not 0 < port < 65536:
            return {"ok": False, "error": "host ou porta inválidos"}
        p = peer.ClientPairing(host, port, self.cert, self.name)
        try:
            info = await asyncio.to_thread(p.start)
        except Exception as e:
            return {"ok": False, "error": str(e) or type(e).__name__}
        if info["fingerprint"] in self.peers.records:
            p.sock.close()
            return {"ok": False, "error": "esse PC já está pareado"}
        pid = new_offer_id()[:8]
        self.peer_pairings[pid] = (p, self.clock() + 60)
        return {"ok": True, "pid": pid, **info}

    async def _peer_confirm(self, req: dict) -> dict:
        entry = self.peer_pairings.pop(str(req.get("pid")), None)
        if entry is None or self.clock() > entry[1]:
            if entry:
                entry[0].sock.close()
            return {"ok": False, "error": "pareamento não encontrado ou expirado"}
        p = entry[0]
        try:
            ok = await asyncio.to_thread(p.finish, bool(req.get("accept")))
        except Exception as e:
            return {"ok": False, "error": str(e) or type(e).__name__}
        if not ok:
            return {"ok": False, "error": "pareamento recusado"}
        self.peers.add(p.fp_s, p.name, "pc", p.server_pem)
        self.peers.update(p.fp_s, "host", p.host)
        self.peers.update(p.fp_s, "port", p.port)
        log_event({"event": "peer.paired", "device_id": p.fp_s})
        return {"ok": True, "device_id": p.fp_s, "name": p.name}

    # ── controle local (CLI) ─────────────────────────────────────────────
    async def _control(self, reader, writer):
        try:
            sock = writer.get_extra_info("socket")
            _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                return
            req = framing.decode_payload(await reader.readexactly(framing.frame_length(await reader.readexactly(4))))
            if req.get("cmd") == "agent.request":
                # o hook pode ser morto (o usuário respondeu no terminal): EOF cancela o pedido e tira o cartão do notch
                cmd, eof = asyncio.ensure_future(self._control_cmd(req)), asyncio.ensure_future(reader.read(1))
                await asyncio.wait({cmd, eof}, return_when=asyncio.FIRST_COMPLETED)
                eof.cancel()
                if not cmd.done():
                    cmd.cancel()
                    return
                resp = cmd.result()
            else:
                resp = await self._control_cmd(req)
            writer.write(framing.encode(resp))
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
            return {"ok": True, "devices": [{**{k: r[k] for k in ("device_id", "name", "kind", "paired_at", "permissions")},
                                             "arrangement": r.get("arrangement"), "screen": r.get("screen"),
                                             "transport": r.get("transport", "adb"), "bt_address": r.get("bt_address"),
                                             "edge_on": r.get("edge_on", True)}
                                            for r in self.store.records.values()]
                               + [{"device_id": r["device_id"], "name": r["name"], "kind": "pc", "paired_at": r["paired_at"],
                                   "permissions": [INPUT_CONTROL], "arrangement": r.get("arrangement"),
                                   "screen": r.get("screen"), "peer": True, "edge_on": r.get("edge_on", True)}
                                  for r in self.peers.records.values()],
                    "input": self._input_status(),
                    "connected": sorted(self.by_device),
                    "audio": self.audio_conn.device_id if self.audio_conn else None}
        if cmd == "devices.revoke":
            await self._input_stop_for(str(req.get("device_id")))
            return {"ok": self.revoke(str(req.get("device_id")))}
        if cmd == "input.status":
            return {"ok": True, **self._input_status()}
        if cmd == "input.bt_pair":
            return await self._bt_pair(str(req.get("device_id")))
        if cmd == "bt.confirm":
            entry = self.bt_confirms.get(str(req.get("pid")))
            if entry is None:
                return {"ok": False, "error": "pedido não encontrado ou expirado"}
            entry["ok"] = req.get("accept") is True
            entry["event"].set()
            return {"ok": True}
        if cmd == "input.screen":
            return await self._input_screen(str(req.get("device_id")))
        if cmd == "input.config":
            return self._input_config(req)
        if cmd == "input.start":
            return await self._input_start(str(req.get("device_id")))
        if cmd == "input.release":  # encerra a borda deste PC e manda para casa quem o controla
            await self._input_stop_for(None)
            self._send_home()
            return {"ok": True}
        if cmd == "peer.pair":
            return await self._peer_pair(req)
        if cmd == "peer.confirm":
            return await self._peer_confirm(req)
        if cmd == "peer.list":
            return {"ok": True, "peers": [{k: r.get(k) for k in ("device_id", "name", "host", "port", "paired_at", "arrangement")}
                                          for r in self.peers.records.values()]}
        if cmd == "peer.remove":
            dev_id = str(req.get("device_id"))
            await self._input_stop_for(dev_id)
            return {"ok": self.peers.remove(dev_id)}
        if cmd == "devices.allow":
            if req.get("action") == INPUT_INJECT:
                self._cancel_sent_home(str(req.get("device_id")))  # liberar de novo cancela o "mandado para casa"
            return self.allow(str(req.get("device_id")), str(req.get("action")))
        if cmd in ("mirror", "input.control"):
            rec = self.store.records.get(str(req.get("device_id")))
            if rec is None:
                return {"ok": False, "error": "dispositivo não pareado"}
            if cmd == "input.control" and INPUT_CONTROL not in rec["permissions"]:
                return {"ok": False, "error": "controle não liberado para este dispositivo"}
            from nexus.actions import desktop
            conn = self.by_device.get(rec["device_id"])
            fn = desktop.mirror if cmd == "mirror" else desktop.control
            return await asyncio.to_thread(fn, rec["name"], conn.ip if conn else None)
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
            # abort, não close: o close do TLS espera o close_notify do outro lado (até 30 s) e o laço ficaria preso
            t["conn"].writer.transport.abort()
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
            if req.get("action") == INPUT_CONTROL:
                await self._input_stop_for(dev_id)
            if req.get("action") == INPUT_INJECT and self.owner and self.owner["device_id"] == dev_id:
                from nexus import kvmwire
                await self._bye(self.owner, kvmwire.BYE_NOT_ALLOWED)
            return {"ok": True}
        if cmd == "agent.request":
            return await self._agent_request(req)
        if cmd == "agent.decide":
            return self._agent_decide(req)
        if cmd == "agent.pending":
            self.agent_ui = True  # só o Balão novo chama: o antigo não mostraria o cartão e o hook esperaria à toa
            return {"ok": True, "requests": [{"id": i, **a["info"]} for i, a in self.agent_requests.items()]}
        if cmd == "actions":
            return {"ok": True, "actions": _phone_actions()}
        if cmd == "status":
            return {"ok": True, "device_id": self.fp_s, "listen": self.listen, "port": self.port}
        return {"ok": False, "error": "comando desconhecido"}


    # ── permissões do Claude Code (hook PermissionRequest; nexus/agentes.py) ──
    async def _agent_request(self, req: dict) -> dict:
        """Mostra o pedido no notch e espera o clique. Nunca executa nada: só devolve a decisão ao hook."""
        from nexus import agentes
        if not self.shell_present():
            self.agent_ui = False
        if not self.agent_ui:
            return {"ok": False, "error": "notch ausente"}  # o terminal pergunta
        if len(self.agent_requests) >= MAX_AGENT_REQUESTS:
            return {"ok": False, "error": "muitos pedidos"}
        try:
            info = agentes.validar(req)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        rid = new_offer_id()
        fut = asyncio.get_running_loop().create_future()
        self.agent_requests[rid] = {"info": info, "fut": fut}
        self.emit({"event": "agent.request", "id": rid, **info})
        decision = None
        try:
            decision = await asyncio.wait_for(fut, self.agent_timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self.agent_requests.pop(rid, None)
            self.emit({"event": "agent.closed", "id": rid})
            # só metadados: nunca o comando nem o caminho
            log_event({"event": "agent.decision", "tool": info["tool"], "decision": decision or "none"})
        return {"ok": True, "decision": None if decision == "terminal" else decision}

    def _agent_decide(self, req: dict) -> dict:
        from nexus import agentes
        a = self.agent_requests.get(str(req.get("id")))
        decision = req.get("decision")
        if a is None or a["fut"].done():
            return {"ok": False, "error": "pedido não encontrado"}
        if decision not in agentes.DECISIONS or (decision == "always" and not a["info"]["always"]):
            return {"ok": False, "error": "decisão inválida"}
        a["fut"].set_result(decision)
        return {"ok": True}


def accel_from(saved):
    """Aceleração salva no registro (de quando houve adb), no formato de kvm.Aceleracao; senão None (padrão)."""
    if isinstance(saved, list) and len(saved) == 4 and all(isinstance(n, (int, float)) for n in saved):
        return tuple(saved)
    if isinstance(saved, dict) and isinstance(saved.get("curve"), list) and saved["curve"]:
        return saved
    return None


def check_arrangement(req: dict) -> dict:
    """Valida o arranjo vindo da UI local: lado, monitor (geometria lógica) e trecho da borda."""
    from nexus.capture import EDGES
    edge, mon = req.get("edge"), req.get("monitor")
    start, end = req.get("start"), req.get("end")
    if edge not in EDGES:
        raise ValueError("lado inválido")
    if (not isinstance(mon, list) or len(mon) != 4 or not all(type(v) is int and abs(v) <= 100000 for v in mon)
            or mon[2] <= 0 or mon[3] <= 0):
        raise ValueError("monitor inválido")
    if not all(type(v) in (int, float) for v in (start, end)) or not 0 <= start < end <= 1 or end - start < 0.05:
        raise ValueError("trecho inválido")
    return {"edge": edge, "monitor": mon, "start": float(start), "end": float(end)}


def new_offer_id() -> str:
    import secrets
    return secrets.token_hex(16)


def _phone_actions() -> list[dict]:
    return [{"id": a.id, "description": a.description, "phone_safe": True,
             "params": [{"name": p.name, "type": p.type, "required": p.required} for p in a.params]}
            for a in catalog.all_actions() if a.phone_safe]


def control(req: dict, path: Path | None = None, timeout: float = 10) -> dict:
    """Cliente do canal de controle (usado pela CLI)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
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
        stop = asyncio.Event()
        import signal
        # o systemd para com SIGTERM: sem isto o Python sai sem o finally (sobravam forwards do adb e sessões)
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        await stop.wait()
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
