import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nexus.catalog import (
    REGISTRY,
    Param,
    Action,
    register,
    get,
    all_actions,
    parse_params,
    run,
    UnknownAction,
    InvalidParams,
    PreconditionFailed,
    NeedsConfirmation,
    ActionTimeout,
)
from nexus.log import state_dir


class TestCatalog(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.environ = dict(os.environ)
        os.environ["NEXUS_STATE_DIR"] = self.tmpdir.name
        self.saved_registry = dict(REGISTRY)

    def tearDown(self):
        REGISTRY.clear()
        REGISTRY.update(self.saved_registry)
        os.environ.clear()
        os.environ.update(self.environ)
        self.tmpdir.cleanup()

    def test_invalid_id(self):
        with self.assertRaises(ValueError):
            register(Action(id="INVALID", description="x", handler=lambda p: p))

    def test_duplicate_id(self):
        register(Action(id="test.dup", description="x", handler=lambda p: p))
        with self.assertRaises(ValueError):
            register(Action(id="test.dup", description="y", handler=lambda p: p))

    def test_phone_safe_dangerous(self):
        with self.assertRaises(ValueError):
            register(
                Action(
                    id="test.sd",
                    description="x",
                    handler=lambda p: p,
                    dangerous=True,
                    phone_safe=True,
                )
            )

    def test_no_equals(self):
        a = Action(id="test.ne", description="x", handler=lambda p: p, params=(Param(name="x"),))
        register(a)
        with self.assertRaises(InvalidParams):
            parse_params(a, ["foo"])

    def test_unknown_param(self):
        a = Action(id="test.up", description="x", handler=lambda p: p, params=(Param(name="x"),))
        register(a)
        with self.assertRaises(InvalidParams):
            parse_params(a, ["y=1"])

    def test_duplicate_param(self):
        a = Action(id="test.dp", description="x", handler=lambda p: p, params=(Param(name="x"),))
        register(a)
        with self.assertRaises(InvalidParams):
            parse_params(a, ["x=1", "x=2"])

    def test_missing_required(self):
        a = Action(id="test.mr", description="x", handler=lambda p: p, params=(Param(name="x"),))
        register(a)
        with self.assertRaises(InvalidParams):
            parse_params(a, [])

    def test_default(self):
        a = Action(
            id="test.def",
            description="x",
            handler=lambda p: p["x"],
            params=(Param(name="x", required=False, default="hi"),),
        )
        register(a)
        self.assertEqual(parse_params(a, []), dict(x="hi"))

    def test_int(self):
        a = Action(id="test.int", description="x", handler=lambda p: p["n"], params=(Param(name="n", type="int"),))
        register(a)
        self.assertEqual(parse_params(a, ["n=42"]), dict(n=42))

    def test_int_invalid(self):
        a = Action(id="test.inti", description="x", handler=lambda p: p["n"], params=(Param(name="n", type="int"),))
        register(a)
        with self.assertRaises(InvalidParams):
            parse_params(a, ["n=abc"])

    def test_bool_valid(self):
        a = Action(id="test.bv", description="x", handler=lambda p: p["f"], params=(Param(name="f", type="bool"),))
        register(a)
        self.assertEqual(parse_params(a, ["f=true"]), dict(f=True))
        self.assertEqual(parse_params(a, ["f=false"]), dict(f=False))
        self.assertEqual(parse_params(a, ["f=1"]), dict(f=True))
        self.assertEqual(parse_params(a, ["f=0"]), dict(f=False))
        self.assertEqual(parse_params(a, ["f=yes"]), dict(f=True))
        self.assertEqual(parse_params(a, ["f=no"]), dict(f=False))

    def test_bool_invalid(self):
        a = Action(id="test.bi", description="x", handler=lambda p: p["f"], params=(Param(name="f", type="bool"),))
        register(a)
        with self.assertRaises(InvalidParams):
            parse_params(a, ["f=maybe"])

    def test_path_not_found(self):
        a = Action(
            id="test.pnf",
            description="x",
            handler=lambda p: p["p"],
            params=(Param(name="p", type="path"),),
        )
        register(a)
        with self.assertRaises(InvalidParams):
            parse_params(a, ["p=/nonexistent/path/test/catalog.py"])

    def test_value_with_equals(self):
        a = Action(id="test.ve", description="x", handler=lambda p: p["t"], params=(Param(name="t"),))
        register(a)
        self.assertEqual(parse_params(a, ["t=a=b"]), dict(t="a=b"))

    def test_needs_confirmation(self):
        a = Action(id="test.nc", description="x", handler=lambda p: p, dangerous=True)
        register(a)
        with self.assertRaises(NeedsConfirmation):
            run("test.nc", dict())

    def test_precondition_fail(self):
        a = Action(id="test.pf", description="x", handler=lambda p: p, precondition=lambda: "fail")
        register(a)
        with self.assertRaises(PreconditionFailed):
            run("test.pf", dict())

    def test_timeout(self):
        a = Action(id="test.to", description="x", handler=lambda p: time.sleep(1), timeout=0.1)
        register(a)
        start = time.monotonic()
        with self.assertRaises(ActionTimeout):
            run("test.to", dict())
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5)

    def test_runtime_error_log(self):
        a = Action(id="test.re", description="x", handler=lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        register(a)
        with self.assertRaises(RuntimeError):
            run("test.re", dict())
        import json
        from pathlib import Path
        events = Path(state_dir() / "events.jsonl").read_text()
        for line in events.strip().split("\n"):
            if line:
                ev = json.loads(line)
                if ev["action"] == "test.re":
                    self.assertEqual(ev["error"], "RuntimeError")
                    self.assertFalse(ev["ok"])

    def test_dangerous_no_confirm_log(self):
        a = Action(id="test.dc", description="x", handler=lambda p: p, dangerous=True)
        register(a)
        with self.assertRaises(NeedsConfirmation):
            run("test.dc", dict())
        import json
        from pathlib import Path
        events = Path(state_dir() / "events.jsonl").read_text()
        for line in events.strip().split("\n"):
            if line:
                ev = json.loads(line)
                if ev["action"] == "test.dc":
                    self.assertEqual(ev["error"], "NeedsConfirmation")

    def test_log_records_keys_not_values(self):
        a = Action(id="test.log", description="x", handler=lambda p: p["secret"], params=(Param(name="secret"),))
        register(a)
        run("test.log", dict(secret="segredo123"))
        import json
        from pathlib import Path
        events = Path(state_dir() / "events.jsonl").read_text()
        for line in events.strip().split("\n"):
            if line:
                ev = json.loads(line)
                if ev["action"] == "test.log":
                    self.assertNotIn("segredo123", ev["params"])
                    self.assertIn("secret", ev["params"])


if __name__ == "__main__":
    unittest.main()
