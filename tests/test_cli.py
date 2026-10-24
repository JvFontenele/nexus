import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nexus.catalog import REGISTRY
from nexus.actions import load_all
from nexus.cli import main


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.environ = dict(os.environ)
        os.environ["NEXUS_STATE_DIR"] = self.tmpdir.name
        load_all()
        self.saved_registry = dict(REGISTRY)

    def tearDown(self):
        REGISTRY.clear()
        REGISTRY.update(self.saved_registry)
        os.environ.clear()
        os.environ.update(self.environ)
        self.tmpdir.cleanup()

    def test_run_echo(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["run", "core.echo", "text=oi"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "oi")

    def test_unknown_action(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["run", "nonexistent"])
        self.assertEqual(rc, 2)

    def test_actions_json(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["actions", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        ids = [item["id"] for item in data]
        self.assertIn("core.echo", ids)


if __name__ == "__main__":
    unittest.main()
