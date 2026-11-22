from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.runtime_config import RuntimeConfig


class RuntimeConfigTests(unittest.TestCase):
    def test_existing_agent_configuration_generates_a_provider_without_storing_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_file = Path(temporary_directory) / "runtime.env"
            config_file.write_text(
                'AGENT_MODEL_URL="https://models.example/v1/chat/completions"\n'
                'AGENT_MODEL_API_KEY="test-private-value"\n'
                'AGENT_MODEL_NAME=example-model\n'
                'DEEPSEEK_API_KEY=old-key\n', encoding="utf-8"
            )

            config = RuntimeConfig.load(config_file)
            patch = config.provider_patch()

            self.assertEqual(config.model_url, "https://models.example/v1")
            self.assertEqual(config.model_name, "example-model")
            self.assertEqual(config.source_path, config_file.resolve())
            self.assertNotIn("test-private-value", str(patch))
            self.assertNotIn("test-private-value", repr(config))
            self.assertEqual(config.redact("error test-private-value"), "error [REDACTED]")
            self.assertEqual(patch[1]["config"]["providers"]["company-agent"]["apiKeyEnv"],
                             "COMPANY_AGENT_MODEL_API_KEY")
            self.assertEqual(
                patch[1]["config"]["providers"]["company-agent"]["models"],
                [{"id": "example-model", "contextWindow": 1048576, "maxTokens": 393216}],
            )

    def test_env_parser_preserves_unicode_and_quoted_comment_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_file = Path(temporary_directory) / "runtime.env"
            config_file.write_text(
                'export AGENT_MODEL_URL="https://models.example/v1" # gateway\n'
                'AGENT_MODEL_API_KEY="key#保留"\n'
                'AGENT_MODEL_NAME="DeepSeek-中文"\n', encoding="utf-8"
            )

            config = RuntimeConfig.load(config_file)

            self.assertEqual(config.api_key, "key#保留")
            self.assertEqual(config.model_name, "DeepSeek-中文")
            self.assertEqual(config.dsh_default_timeout_seconds, 3600.0)
            self.assertEqual(config.dsh_max_timeout_seconds, 7200.0)
            self.assertEqual(config.dsh_idle_timeout_seconds, 600.0)
            self.assertEqual(config.dsh_compaction_timeout_seconds, 600.0)
            self.assertEqual(config.dsh_soft_wrapup_seconds, 300.0)

    def test_dsh_timeout_settings_are_configurable_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_file = Path(temporary_directory) / "runtime.env"
            config_file.write_text(
                "AGENT_MODEL_URL=https://models.example/v1\n"
                "AGENT_MODEL_API_KEY=secret\n"
                "AGENT_MODEL_NAME=model\n"
                "AGENT_DSH_MAX_TIMEOUT_SECONDS=7200\n"
                "AGENT_DSH_TIMEOUT_SECONDS=5400\n"
                "AGENT_DSH_IDLE_TIMEOUT_SECONDS=90\n"
                "AGENT_DSH_COMPACTION_TIMEOUT_SECONDS=900\n"
                "AGENT_DSH_SOFT_WRAPUP_SECONDS=240\n",
                encoding="utf-8",
            )
            config = RuntimeConfig.load(config_file)
            self.assertEqual(config.dsh_max_timeout_seconds, 7200.0)
            self.assertEqual(config.dsh_default_timeout_seconds, 5400.0)
            self.assertEqual(config.dsh_idle_timeout_seconds, 90.0)
            self.assertEqual(config.dsh_compaction_timeout_seconds, 900.0)
            self.assertEqual(config.dsh_soft_wrapup_seconds, 240.0)

            config_file.write_text(
                "AGENT_MODEL_URL=https://models.example/v1\n"
                "AGENT_MODEL_API_KEY=secret\n"
                "AGENT_MODEL_NAME=model\n"
                "AGENT_DSH_MAX_TIMEOUT_SECONDS=1800\n"
                "AGENT_DSH_TIMEOUT_SECONDS=2000\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "AGENT_DSH_TIMEOUT_SECONDS"):
                RuntimeConfig.load(config_file)


if __name__ == "__main__":
    unittest.main()
