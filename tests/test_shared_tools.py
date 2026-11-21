from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.shared_tools import SharedToolsError, SharedToolsManager


class SharedToolsTests(unittest.TestCase):
    def test_script_crud_and_layer_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = SharedToolsManager(Path(directory) / "shared-tools")
            manager.ensure_layers()
            saved = manager.save_script("common", "hello.py", "#!/usr/bin/env python3\nprint('hi')",
                                        description="Greeting helper")
            self.assertEqual(saved["layer"], "common")
            self.assertEqual(saved["name"], "hello.py")
            read_back = manager.read_script("common", "hello.py")
            self.assertIn("print('hi')", read_back["content"])
            if os.name != "nt":
                self.assertTrue(os.access(Path(saved["path"]), os.X_OK))
            summary = manager.summary()
            self.assertEqual([item["name"] for item in summary["layers"]["common"]["scripts"]], ["hello.py"])
            self.assertEqual(summary["layers"]["internal"]["scripts"], [])
            self.assertTrue(manager.delete_script("common", "hello.py"))
            self.assertFalse(manager.delete_script("common", "hello.py"))

    def test_rejects_unsafe_script_names_and_package_specs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = SharedToolsManager(Path(directory) / "shared-tools")
            with self.assertRaises(SharedToolsError):
                manager.save_script("other", "x.py", "print(1)")
            with self.assertRaises(SharedToolsError):
                manager.save_script("common", "../escape.py", "print(1)")
            with self.assertRaises(SharedToolsError):
                manager.install_python_packages("common", ["--index-url=https://bad.example/simple"])
            with self.assertRaises(SharedToolsError):
                manager.install_python_packages("common", ["requests && rm -rf /"])

    def test_install_python_packages_invokes_pip_target_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = SharedToolsManager(Path(directory) / "shared-tools")
            captured: list[list[str]] = []

            def fake_pip(command: list[str], _timeout: float):
                captured.append(command)
                return SimpleNamespace(returncode=0, stdout="Successfully installed httpx-0.28.0", stderr="")

            result = manager.install_python_packages("internal", ["httpx>=0.27"], pip_runner=fake_pip)
            self.assertEqual(result["layer"], "internal")
            self.assertEqual(result["packages"], ["httpx>=0.27"])
            self.assertIn("--target", captured[0])
            self.assertIn(str((Path(directory) / "shared-tools" / "internal" / "python").resolve()), captured[0])
            self.assertEqual(manager.summary()["layers"]["internal"]["packages"][0]["spec"], "httpx>=0.27")


if __name__ == "__main__":
    unittest.main()
