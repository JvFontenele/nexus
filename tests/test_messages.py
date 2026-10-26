import copy
import unittest

from nexus.proto.messages import SCHEMAS, ProtocolError, make, validate

H = "a" * 64
VALID = {
    "hello": {"device_id": H, "name": "POCO", "kind": "phone", "app_version": "0.1", "protocol": [1]},
    "pair.request": {"commit": H},
    "pair.challenge": {"session": "s", "n_s": H, "expires_in": 60},
    "pair.reveal": {"session": "s", "n_c": H},
    "pair.confirm": {"session": "s", "accept": True},
    "pair.result": {"ok": True},
    "unpair": {},
    "ping": {},
    "pong": {},
    "actions.list": {},
    "actions.result": {"actions": []},
    "action.run": {"action": "core.echo", "params": {"text": "oi", "n": 1, "b": False}},
    "action.result": {"ok": True, "result": {"x": [1, None]}},
    "share.text": {"text": "oi"},
    "clipboard.set": {"text": "oi"},
    "open.url": {"url": "https://exemplo.com"},
    "mirror.request": {},
    "file.push": {"name": "foto.jpg", "size": 10},
    "file.offer": {"id": "x", "name": "a.pdf", "size": 3},
    "file.pull": {"id": "x"},
    "file.ready": {},
    "audio.attach": {},
    "audio.start": {},
    "audio.stop": {},
    "audio.ready": {"rate": 48000, "channels": 2, "format": "s16le"},
    "audio.push": {"rate": 48000, "channels": 2, "format": "s16le"},
    "devices.peers": {},
    "devices.peers.result": {"devices": []},
    "relay": {"to": "a" * 64, "kind": "text", "value": "oi"},
    "audio.in.start": {},
    "audio.in.stop": {},
    "input.attach": {},
    "input.ready": {"zones": [[0, 0, 1920, 1080], [1920, 0, 1920, 1080]]},
    "error": {"code": "x", "message": "y"},
}


def env(type_, body, **extra):
    return {"v": 1, "id": "1", "type": type_, "body": body, **extra}


class TestValidate(unittest.TestCase):
    def code(self, msg):
        with self.assertRaises(ProtocolError) as ctx:
            validate(msg)
        return ctx.exception.code

    def test_every_type_valid(self):
        self.assertEqual(set(VALID), set(SCHEMAS))
        for t, body in VALID.items():
            with self.subTest(t):
                validate(env(t, copy.deepcopy(body)))

    def test_envelope(self):
        self.assertEqual(self.code(env("ping", {}, extra=1)), "bad_envelope")
        self.assertEqual(self.code({"v": 1, "id": "1", "type": "ping"}), "bad_envelope")
        self.assertEqual(self.code({**env("ping", {}), "v": True}), "bad_version")
        self.assertEqual(self.code({**env("ping", {}), "v": 2}), "bad_version")
        self.assertEqual(self.code({**env("ping", {}), "id": ""}), "bad_envelope")
        self.assertEqual(self.code({**env("ping", {}), "id": "x" * 65}), "bad_envelope")
        self.assertEqual(self.code(env("ping", {}, re="")), "bad_envelope")
        validate(env("pong", {}, re="abc"))
        self.assertEqual(self.code(env("shell", {})), "unknown_type")
        self.assertEqual(self.code(env("ping", [])), "bad_body")

    def test_body(self):
        self.assertEqual(self.code(env("ping", {"x": 1})), "bad_body")
        self.assertEqual(self.code(env("share.text", {})), "bad_body")
        self.assertEqual(self.code(env("share.text", {"text": 1})), "bad_body")
        validate(env("pair.result", {"ok": False, "reason": "expired"}))
        validate(env("hello", {**VALID["hello"], "cert_pem": "PEM"}))

    def test_bool_is_not_int(self):
        b = {**VALID["pair.challenge"], "expires_in": True}
        self.assertEqual(self.code(env("pair.challenge", b)), "bad_body")
        self.assertEqual(self.code(env("pair.confirm", {"session": "s", "accept": 1})), "bad_body")

    def test_hex_fields(self):
        for bad in ("A" * 64, "a" * 63, "a" * 64 + "\n", "g" * 64):
            with self.subTest(bad):
                self.assertEqual(self.code(env("pair.request", {"commit": bad})), "bad_body")
        self.assertEqual(self.code(env("hello", {**VALID["hello"], "device_id": "x"})), "bad_body")

    def test_kind(self):
        self.assertEqual(self.code(env("hello", {**VALID["hello"], "kind": "tv"})), "bad_body")

    def test_input_ready_zones(self):
        for bad in ([], [[0, 0, 0, 10]], [[0, 0, 10]], [[0, 0, 1.5, 10]], [[0, 0, True, 10]], [[0, 0, 10, 10**9]],
                    [[0, 0, 10, 10]] * 17, ["x"]):
            with self.subTest(bad):
                self.assertEqual(self.code(env("input.ready", {"zones": bad})), "bad_body")
        self.assertEqual(self.code(env("input.attach", {"x": 1})), "bad_body")

    def test_params(self):
        for bad in ({"a": {"b": 1}}, {"a": [1]}, {"a": 1.5}, {"a": None}):
            with self.subTest(bad):
                self.assertEqual(self.code(env("action.run", {"action": "x", "params": bad})), "bad_body")

    def test_too_large(self):
        big = "a" * 65537
        self.assertEqual(self.code(env("share.text", {"text": big})), "too_large")
        self.assertEqual(self.code(env("action.result", {"ok": True, "result": [big]})), "too_large")
        validate(env("share.text", {"text": "a" * 65536}))


class TestMake(unittest.TestCase):
    def test_unique_and_valid(self):
        a, b = make("ping", {}), make("pong", {}, re="x")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(b["re"], "x")
        self.assertNotIn("re", a)

    def test_rejects_invalid(self):
        with self.assertRaises(ProtocolError):
            make("ping", {"x": 1})


if __name__ == "__main__":
    unittest.main()
