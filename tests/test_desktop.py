import subprocess
import unittest
from unittest import mock

from nexus.actions import desktop

OUT = """List of devices attached
7db4da6e               device usb:1-3 product:redwood_global model:22101320G device:redwood transport_id:1
192.168.1.11:41235     offline product:x model:22101320G device:y
adb-XYZ._adb-tls-connect._tcp device product:t model:SM_X200 device:t
evil;rm                device model:Ruim
"""


class TestAdbSerial(unittest.TestCase):
    def run_with(self, model):
        res = subprocess.CompletedProcess([], 0, stdout=OUT)
        with mock.patch("subprocess.run", return_value=res):
            return desktop.adb_serial(model)

    def test_match_and_filters(self):
        self.assertEqual(self.run_with("22101320G"), "7db4da6e")  # offline ignorado
        self.assertEqual(self.run_with("SM_X200"), "adb-XYZ._adb-tls-connect._tcp")
        self.assertIsNone(self.run_with("Ruim"))  # serial com caractere inválido
        self.assertIsNone(self.run_with("2210"))  # só casa o modelo inteiro

    def test_mirror_without_adb_device(self):
        with mock.patch("shutil.which", return_value="/bin/x"), mock.patch.object(desktop, "adb_serial", return_value=None):
            self.assertFalse(desktop.mirror("x")["ok"])


class TestAdbWifi(unittest.TestCase):
    def test_only_connects_to_paired_device_ip(self):
        anuncios = [("10.0.0.99", 40000), ("10.0.0.5", 41000)]
        with mock.patch("nexus.mdns.browse", return_value=anuncios), mock.patch("subprocess.run") as run:
            desktop.adb_wifi_connect("10.0.0.5")
            run.assert_called_once()
            self.assertEqual(run.call_args[0][0], ["adb", "connect", "10.0.0.5:41000"])
            run.reset_mock()
            desktop.adb_wifi_connect("10.0.0.7")  # ninguém anuncia esse IP: não conecta em nada
            run.assert_not_called()


class TestAudio(unittest.TestCase):
    def test_node_id_with_concatenated_json(self):
        from nexus import audio
        dump = '[{"id": 7, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "nexus-audio"}}}]\n[{"id": 9}]'
        with mock.patch.object(audio, "_run", return_value=dump):
            self.assertEqual(audio.node_id("nexus-audio"), 7)

    def test_start_rolls_back_on_failure(self):
        from nexus import audio
        calls = []
        with mock.patch.object(audio, "_run", side_effect=lambda argv: calls.append(argv) or "[]"), \
                mock.patch.object(audio, "default_sink", return_value=54):
            with self.assertRaises(RuntimeError):
                audio.start("POCO")
        self.assertIn(["wpctl", "set-default", "54"], calls)  # saída antiga restaurada


class TestFiles(unittest.TestCase):
    def test_safe_name(self):
        from nexus.files import safe_name
        self.assertEqual(safe_name("../../.bashrc"), "bashrc")
        self.assertEqual(safe_name("C:\\Users\\x\\foto.jpg"), "foto.jpg")
        self.assertEqual(safe_name("a\x00b\nc.txt"), "abc.txt")
        self.assertEqual(safe_name("..."), "arquivo")
        self.assertEqual(safe_name('rel<at>"o|rio?.pdf'), "relatorio.pdf")
        self.assertLessEqual(len(safe_name("x" * 500)), 150)

    def test_unique_path(self):
        import tempfile
        from pathlib import Path
        from nexus.files import unique_path
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "foto.jpg").write_bytes(b"")
            (d / "foto (1).jpg.part").write_bytes(b"")
            self.assertEqual(unique_path(d, "foto.jpg").name, "foto (2).jpg")


if __name__ == "__main__":
    unittest.main()
