"""Permissões do Claude Code pelo notch: hook, fila no daemon, decisão só para o id certo, expiração e instalação."""
import asyncio
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from nexus import agentes
from nexus.daemon import Daemon, control_path

RAIZ = Path(__file__).resolve().parent.parent
EVENTO = {
    "session_id": "abc12345xyz", "cwd": "/home/u/projetos/loja", "hook_event_name": "PermissionRequest",
    "tool_name": "Bash", "tool_input": {"command": "rm -rf node_modules", "description": "Limpa"},
    "permission_suggestions": [
        {"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "rm -rf node_modules"}],
         "behavior": "allow", "destination": "localSettings"},
        {"type": "setMode", "mode": "bypassPermissions", "destination": "session"},
        {"type": "addRules", "rules": [{"toolName": "Bash"}], "behavior": "deny", "destination": "session"},
    ],
}


class TestResumo(unittest.TestCase):
    def test_resumo_e_sempre_so_addrules_allow(self):
        r = agentes.resumir(EVENTO)
        self.assertEqual((r["tool"], r["project"], r["detail"], r["session"]), ("Bash", "loja", "rm -rf node_modules", "abc12345"))
        self.assertEqual(r["always"], ["Bash(rm -rf node_modules)"])
        self.assertEqual(agentes.regras_sempre(EVENTO["permission_suggestions"]), [EVENTO["permission_suggestions"][0]])
        self.assertEqual(agentes.validar(r), r)

    def test_detalhe_truncado_e_campos_estranhos(self):
        r = agentes.resumir({"tool_name": "Write", "tool_input": {"file_path": "/a/b.py", "content": "x" * 10**6}})
        self.assertEqual(r["detail"], "/a/b.py")
        r = agentes.resumir({"tool_name": 5, "tool_input": {"x": "y" * 10**5}, "cwd": ["?"]})
        self.assertEqual((r["tool"], r["project"]), ("?", ""))
        self.assertLessEqual(len(r["detail"]), agentes.MAX_DETAIL)
        with self.assertRaises(ValueError):
            agentes.validar({**r, "detail": "x" * (agentes.MAX_DETAIL + 1)})
        with self.assertRaises(ValueError):
            agentes.validar({**r, "always": [{"toolName": "Bash"}]})

    def test_saida(self):
        sempre = agentes.regras_sempre(EVENTO["permission_suggestions"])
        self.assertEqual(agentes.saida(None, sempre), "")
        self.assertEqual(agentes.saida("terminal", sempre), "")
        self.assertEqual(agentes.saida("always", []), "")  # sem sugestão, "sempre" não vira "permitir"
        d = json.loads(agentes.saida("always", sempre))["hookSpecificOutput"]
        self.assertEqual(d["hookEventName"], "PermissionRequest")
        self.assertEqual(d["decision"], {"behavior": "allow", "updatedPermissions": sempre})
        self.assertEqual(json.loads(agentes.saida("deny", []))["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertNotIn("updatedPermissions", json.loads(agentes.saida("allow", sempre))["hookSpecificOutput"]["decision"])

    def test_socket_igual_ao_do_daemon(self):
        for env in ({"NEXUS_RUNTIME_DIR": "/r"}, {"XDG_RUNTIME_DIR": "/x"}, {"NEXUS_DATA_DIR": "/d"}, {"XDG_DATA_HOME": "/h"}):
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(agentes._socket_controle(), control_path(), env)

    def test_hook_sem_daemon_como_script(self):
        # como o Claude Code roda: processo novo, daemon ausente → exit 0 sem saída, rápido
        env = {**os.environ, "NEXUS_RUNTIME_DIR": tempfile.mkdtemp()}
        t = time.monotonic()
        p = subprocess.run([sys.executable, "-P", str(RAIZ / "nexus" / "agentes.py"), "hook"], input=json.dumps(EVENTO),
                           capture_output=True, text=True, env=env, timeout=10, cwd="/")
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))
        self.assertLess(time.monotonic() - t, 3)
        self.assertEqual(agentes.hook("isto não é json"), "")
        self.assertEqual(agentes.hook(json.dumps({**EVENTO, "hook_event_name": "PreToolUse"})), "")


class TestDaemonAgentes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.estado = mock.patch.dict(os.environ, {"NEXUS_STATE_DIR": str(Path(cls.tmp.name) / "state")})
        cls.estado.start()
        cls.loop = asyncio.new_event_loop()
        threading.Thread(target=cls.loop.run_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.estado.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.data = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.d = Daemon(self.data, ["127.0.0.1"], 0)
        self.d.shell_present = lambda: True
        self.eventos = []
        self.d.emit = self.eventos.append
        self.sock = self.data / "run" / "control.sock"
        self.call(self.d.start_control(self.sock))
        self.cmd({"cmd": "agent.pending"})  # o notch se anuncia

    def tearDown(self):
        self.call(self.d.close())

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def cmd(self, req):
        return self.call(self.d._control_cmd(req))

    def hook_em_thread(self, evento=EVENTO):
        out = {}
        t = threading.Thread(target=lambda: out.update(texto=agentes.hook(json.dumps(evento), self.sock)))
        t.start()
        return t, out

    def esperar_pendentes(self, n):
        for _ in range(200):
            p = self.cmd({"cmd": "agent.pending"})["requests"]
            if len(p) >= n:
                return p
            time.sleep(0.01)
        self.fail(f"esperava {n} pedidos")

    def test_permitir_sempre_e_id_errado(self):
        t, out = self.hook_em_thread()
        [p] = self.esperar_pendentes(1)
        self.assertEqual((p["tool"], p["detail"], p["always"]), ("Bash", "rm -rf node_modules", ["Bash(rm -rf node_modules)"]))
        self.assertEqual(self.eventos[0]["event"], "agent.request")
        self.assertFalse(self.cmd({"cmd": "agent.decide", "id": "0" * 32, "decision": "allow"})["ok"])
        self.assertFalse(self.cmd({"cmd": "agent.decide", "id": p["id"], "decision": "talvez"})["ok"])
        self.assertTrue(t.is_alive())  # id errado ou decisão inválida não decidem nada
        self.assertTrue(self.cmd({"cmd": "agent.decide", "id": p["id"], "decision": "always"})["ok"])
        t.join(5)
        d = json.loads(out["texto"])["hookSpecificOutput"]["decision"]
        self.assertEqual(d["behavior"], "allow")
        self.assertEqual(d["updatedPermissions"][0]["rules"], [{"toolName": "Bash", "ruleContent": "rm -rf node_modules"}])
        self.assertFalse(self.cmd({"cmd": "agent.decide", "id": p["id"], "decision": "allow"})["ok"])  # uso único
        self.assertEqual(self.eventos[-1], {"event": "agent.closed", "id": p["id"]})
        log = (Path(os.environ["NEXUS_STATE_DIR"]) / "events.jsonl").read_text()
        self.assertNotIn("node_modules", log)  # só metadados no log

    def test_fila_negar_e_terminal(self):
        sem_sugestao = {**EVENTO, "permission_suggestions": []}
        (t1, o1), (t2, o2) = self.hook_em_thread(), self.hook_em_thread(sem_sugestao)
        p1, p2 = self.esperar_pendentes(2)
        self.assertNotEqual(p1["id"], p2["id"])
        sem = p1 if not p1["always"] else p2
        self.assertFalse(self.cmd({"cmd": "agent.decide", "id": sem["id"], "decision": "always"})["ok"])
        self.assertTrue(self.cmd({"cmd": "agent.decide", "id": p1["id"], "decision": "deny"})["ok"])
        self.assertTrue(self.cmd({"cmd": "agent.decide", "id": p2["id"], "decision": "terminal"})["ok"])
        t1.join(5)
        t2.join(5)
        self.assertEqual(json.loads(o1["texto"])["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertEqual(o2["texto"], "")  # responder no terminal = sem decisão

    def test_expira_sem_decidir(self):
        self.d.agent_timeout = 0.2
        t, out = self.hook_em_thread()
        t.join(5)
        self.assertEqual(out["texto"], "")
        self.assertEqual(self.cmd({"cmd": "agent.pending"})["requests"], [])

    def test_hook_morto_tira_o_pedido(self):
        import socket
        from nexus.proto import framing
        s = socket.socket(socket.AF_UNIX)
        s.connect(str(self.sock))
        s.sendall(framing.encode({"cmd": "agent.request", **agentes.resumir(EVENTO)}))
        self.esperar_pendentes(1)
        s.close()  # o Claude Code matou o hook (respondido no terminal)
        for _ in range(200):
            if not self.cmd({"cmd": "agent.pending"})["requests"]:
                break
            time.sleep(0.01)
        self.assertEqual(self.cmd({"cmd": "agent.pending"})["requests"], [])
        self.assertEqual(self.eventos[-1]["event"], "agent.closed")

    def test_sem_notch_nao_espera(self):
        self.d.shell_present = lambda: False
        t, out = self.hook_em_thread()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(out["texto"], "")
        self.assertFalse(self.cmd({"cmd": "agent.request", "tool": 1})["ok"])

    def test_balao_antigo_nao_espera(self):
        self.d.agent_ui = False  # Shell no ar, mas o Balão nunca pediu agent.pending
        t, out = self.hook_em_thread()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(out["texto"], "")


class TestInstalar(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg = self.dir / "settings.json"
        self.original = {"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "rtk hook claude"}]}]}}
        self.cfg.write_text(json.dumps(self.original, indent=2) + "\n")
        self.saidas = []

    def instalar(self, gravar):
        return agentes.instalar(self.dir, gravar, datetime.datetime(2026, 10, 26, 9, 5, 7), self.saidas.append)

    def test_diff_sem_gravar_depois_grava_com_backup(self):
        antes = self.cfg.read_text()
        self.assertFalse(agentes.instalado(self.dir))
        self.assertEqual(self.instalar(False), 0)
        self.assertEqual(self.cfg.read_text(), antes)
        self.assertIn('+    "PermissionRequest": [', self.saidas[0])
        self.assertEqual(self.instalar(True), 0)
        self.assertEqual((self.dir / "settings.json.bak-20261026-090507").read_text(), antes)
        novo = json.loads(self.cfg.read_text())
        self.assertEqual(novo["hooks"]["PreToolUse"], self.original["hooks"]["PreToolUse"])
        self.assertEqual(novo["model"], "opus")
        [grupo] = novo["hooks"]["PermissionRequest"]
        self.assertEqual(grupo["hooks"][0]["command"], agentes.comando_hook())
        self.assertTrue(agentes.instalado(self.dir))
        self.saidas.clear()
        self.assertEqual(self.instalar(True), 0)  # idempotente
        self.assertIn("já está instalado", self.saidas[0])

    def test_json_invalido_nao_grava(self):
        self.cfg.write_text("{ quebrado")
        self.assertEqual(self.instalar(True), 1)
        self.assertEqual(self.cfg.read_text(), "{ quebrado")
        self.cfg.write_text('{"hooks": []}')
        self.assertEqual(self.instalar(True), 1)
        self.assertEqual(list(self.dir.iterdir()), [self.cfg])  # nem backup


if __name__ == "__main__":
    unittest.main()
