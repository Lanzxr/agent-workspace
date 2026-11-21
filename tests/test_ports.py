from __future__ import annotations

import socket
import tempfile
import unittest
from pathlib import Path

from app.knowledge.config import KnowledgeSettings
from app.ports import (
    DEFAULT_ADMIN_PORT,
    DEFAULT_ANYTHINGLLM_PORT,
    DEFAULT_DSSUPERAGENT_PORT,
    DEFAULT_FORGE_MCP_PORT,
    DEFAULT_KNOWLEDGE_API_PORT,
    DEFAULT_PORT_BASE,
    DEFAULT_SEARCH_PORT,
    DEFAULT_WEB_GATE_PORT,
    assert_port_available,
    resolve_port_settings,
)
from app.runtime_config import RuntimeConfig, parse_forge_mcp_settings
from app.web_gate import WebGateSettings


class PortSettingsTests(unittest.TestCase):
    def test_defaults_use_low_conflict_contiguous_block(self) -> None:
        ports = resolve_port_settings({})
        self.assertEqual(ports.port_base, DEFAULT_PORT_BASE)
        self.assertEqual(
            (
                ports.admin_port,
                ports.knowledge_api_port,
                ports.web_gate_port,
                ports.forge_mcp_port,
                ports.dssuperagent_port,
                ports.search_port,
                ports.anythingllm_port,
            ),
            (
                DEFAULT_ADMIN_PORT,
                DEFAULT_KNOWLEDGE_API_PORT,
                DEFAULT_WEB_GATE_PORT,
                DEFAULT_FORGE_MCP_PORT,
                DEFAULT_DSSUPERAGENT_PORT,
                DEFAULT_SEARCH_PORT,
                DEFAULT_ANYTHINGLLM_PORT,
            ),
        )
        self.assertEqual(ports.dssuperagent_url, "http://127.0.0.1:28769")
        self.assertEqual(ports.knowledge_api_url, "http://127.0.0.1:28766/internal/knowledge/search")
        self.assertEqual(ports.web_gate_url, "http://127.0.0.1:28767/internal/web/authorize")
        self.assertEqual(ports.web_gate_dsh_search_url, "http://127.0.0.1:28767/internal/web/dsh-search")
        self.assertEqual(ports.web_search_url, "http://127.0.0.1:28770/search")
        self.assertEqual(ports.anythingllm_url, "http://127.0.0.1:28771")

    def test_port_base_shifts_all_services_and_auto_derives_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "runtime.env"
            env_path.write_text(
                "\n".join([
                    "AGENT_MODEL_URL=https://api.example.com/v1",
                    "AGENT_MODEL_API_KEY=model-key-123",
                    "AGENT_MODEL_NAME=deepseek-chat",
                    "COMPANY_AGENT_PORT_BASE=29765",
                    "ANYTHINGLLM_API_KEY=allm-key",
                    "COMPANY_AGENT_KNOWLEDGE_GRANT_SECRET=" + ("g" * 40),
                ]),
                encoding="utf-8",
            )
            ports = resolve_port_settings(RuntimeConfig._parse_env(env_path))
            self.assertEqual(ports.admin_port, 29765)
            self.assertEqual(ports.knowledge_api_port, 29766)
            self.assertEqual(ports.web_gate_port, 29767)
            self.assertEqual(ports.forge_mcp_port, 29768)
            self.assertEqual(ports.dssuperagent_port, 29769)
            self.assertEqual(ports.search_port, 29770)
            self.assertEqual(ports.anythingllm_port, 29771)

            cfg = RuntimeConfig.load(env_path)
            self.assertEqual(cfg.forge_mcp_port, 29768)
            self.assertEqual(cfg.web_gate_search_url, "http://127.0.0.1:29767/internal/web/dsh-search")

            gate = WebGateSettings.load(env_path)
            self.assertEqual(gate.port, 29767)
            self.assertEqual(gate.search_url, "http://127.0.0.1:29770/search")

            kb = KnowledgeSettings.load_optional(env_path)
            self.assertIsNotNone(kb)
            assert kb is not None
            self.assertEqual(kb.query_api_port, 29766)
            self.assertEqual(kb.base_url, "http://127.0.0.1:29771")

            self.assertEqual(parse_forge_mcp_settings(RuntimeConfig._parse_env(env_path))[2], 29768)

    def test_explicit_legacy_urls_preserve_port_numbers_and_detect_duplicates(self) -> None:
        ports = resolve_port_settings({
            "COMPANY_AGENT_KNOWLEDGE_API_PORT": "8766",
            "COMPANY_AGENT_WEB_GATE_PORT": "8767",
            "COMPANY_AGENT_WEB_GATE_URL": "http://127.0.0.1:8767/internal/web/authorize",
            "COMPANY_AGENT_WEB_SEARCH_URL": "http://127.0.0.1:8888/search",
            "ANYTHINGLLM_BASE_URL": "http://127.0.0.1:3001",
        })
        self.assertEqual(ports.search_port, 8888)
        self.assertEqual(ports.anythingllm_port, 3001)
        self.assertEqual(ports.admin_port, 28765)

        with self.assertRaises(ValueError):
            resolve_port_settings({
                "COMPANY_AGENT_ADMIN_PORT": "29000",
                "COMPANY_AGENT_WEB_GATE_PORT": "29000",
            })

        with self.assertRaises(ValueError):
            resolve_port_settings({"COMPANY_AGENT_PORT_BASE": "65530"})

    def test_assert_port_available_reports_occupied_port_actionably(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            occupied_port = int(holder.getsockname()[1])
            with self.assertRaises(RuntimeError) as ctx:
                assert_port_available(
                    "127.0.0.1",
                    occupied_port,
                    service_name="Admin Console",
                    env_var="COMPANY_AGENT_ADMIN_PORT",
                )
            msg = str(ctx.exception)
            self.assertIn(str(occupied_port), msg)
            self.assertIn("COMPANY_AGENT_ADMIN_PORT", msg)
            self.assertIn("COMPANY_AGENT_PORT_BASE", msg)
