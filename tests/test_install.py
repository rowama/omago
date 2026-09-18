import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class InstallTests(unittest.TestCase):
    def test_install_space_in_path_and_upgrade_preserves_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "an install"
            bindir = Path(directory) / "commands"
            source = Path(__file__).resolve().parents[1]
            command = [sys.executable, str(source / "install.py"), "--root", str(root), "--bin-dir", str(bindir)]
            subprocess.run(command, check=True, capture_output=True)
            executable = bindir / "omago"
            result = subprocess.run([executable, "--version"], check=True, capture_output=True, text=True)
            self.assertIn("Omago 0.1.0", result.stdout)
            self.assertFalse((root / "state").exists())
            self.assertFalse((root / "machine.json").exists())
            settings = root / "settings.json"
            original = settings.read_text().replace('"auto"', '"ssh"')
            settings.write_text(original)
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(settings.read_text(), original)

    def test_occupied_launcher_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "install"
            root.mkdir()
            (root / "omago").write_text("my important file")
            source = Path(__file__).resolve().parents[1]
            result = subprocess.run([sys.executable, str(source / "install.py"), "--root", str(root),
                                     "--bin-dir", str(Path(directory) / "bin")], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / "omago").read_text(), "my important file")


if __name__ == "__main__":
    unittest.main()
