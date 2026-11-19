from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_release = load_script("company_agent_build_release", "scripts/build-release.py")
install_release = load_script("company_agent_install_release", "scripts/install-release.py")


class ReleaseMetadataTests(unittest.TestCase):
    def test_runtime_metadata_records_the_stable_dsh_package_paths(self) -> None:
        metadata = build_release.runtime_metadata(
            "/home/lan_z/node-v24/bin/node",
            "/home/lan_z/.npm/_npx/cache/node_modules/@deepseek-ai/dsh/lib/bin.js",
            "/home/lan_z/superagent-home",
        )
        self.assertEqual(metadata["dsh_node_modules"], "/home/lan_z/.npm/_npx/cache/node_modules")
        self.assertNotIn("/tmp/", metadata["dsh_bin"])

    def test_runtime_metadata_rejects_ephemeral_dsh_entrypoints(self) -> None:
        with self.assertRaisesRegex(ValueError, "stable DSH package entrypoint"):
            build_release.runtime_metadata(
                "/home/lan_z/node-v24/bin/node",
                "/tmp/company-agent-run-bin/dsh",
                "/home/lan_z/superagent-home",
            )

    def test_runtime_metadata_requires_absolute_linux_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute WSL/Linux path"):
            build_release.runtime_metadata("node", "/home/lan_z/dsh/bin.js", "/home/lan_z/dsh-home")


class RuntimeConfigPreservationTests(unittest.TestCase):
    def test_validation_reads_the_private_config_without_rewriting_it(self) -> None:
        content = b"COMPANY_AGENT_ADMIN_TOKEN=private-value\nAGENT_MODEL_URL=https://example.invalid/v1\n"
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "runtime.env"
            config.write_bytes(content)
            config.chmod(0o600)
            values = install_release.validate_runtime_config(config)
            self.assertEqual(values["COMPANY_AGENT_ADMIN_TOKEN"], "private-value")
            self.assertEqual(config.read_bytes(), content)

    def test_validation_rejects_a_config_without_the_admin_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "runtime.env"
            config.write_text("AGENT_MODEL_URL=https://example.invalid/v1\n", encoding="utf-8")
            config.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "admin token is missing"):
                install_release.validate_runtime_config(config)


if __name__ == "__main__":
    unittest.main()
