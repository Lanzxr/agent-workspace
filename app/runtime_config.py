from __future__ import annotations

import json
import math
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .mcp_registry import McpServerSpec, load_mcp_secrets, parse_mcp_servers_json
from .ports import DEFAULT_FORGE_MCP_PORT, resolve_port_settings


def parse_forge_mcp_settings(values: Mapping[str, str]) -> tuple[bool, str, int, str]:
    """Parse shared Tool Forge loopback MCP server settings with strict validation."""
    enabled = str(values.get("COMPANY_AGENT_FORGE_MCP_ENABLED") or "true").strip().lower() in {"1", "true", "yes", "on"}
    ports = resolve_port_settings(values)
    host = ports.forge_mcp_host
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("COMPANY_AGENT_FORGE_MCP_HOST must bind to 127.0.0.1 or localhost")
    owner = str(values.get("COMPANY_AGENT_FORGE_MCP_OWNER") or "admin").strip().lower() or "admin"
    if owner not in {"admin", "qq"}:
        raise ValueError("COMPANY_AGENT_FORGE_MCP_OWNER must be admin or qq")
    return enabled, host, ports.forge_mcp_port, owner

DSH_VERSION = "0.2.2-rc.1"
POLICY_VERSION = "workspace-dev-v1"


@dataclass(frozen=True)
class QQBotSettings:
    app_id: str
    client_secret: str = field(repr=False)
    allowed_publications: frozenset[str] = frozenset()
    api_base_url: str = "https://api.bot.qq.com"
    auto_execute_capabilities: frozenset[str] = frozenset()
    workspace_id: str | None = None
    isolate_workspaces: bool = True

    @classmethod
    def load(cls, path: Path) -> "QQBotSettings":
        if not path.is_file():
            raise ValueError(f"runtime configuration not found: {path}")
        values = RuntimeConfig._parse_env(path)
        app_id = (values.get("QQBOT_APP_ID") or values.get("QQBOT_APPID") or "").strip()
        client_secret = (values.get("QQBOT_CLIENT_SECRET") or values.get("QQBOT_SECRET") or "").strip()
        if not app_id or not client_secret:
            raise ValueError("QQBOT_APP_ID and QQBOT_CLIENT_SECRET are required")
        if any(character in app_id + client_secret for character in "\r\n\x00"):
            raise ValueError("QQ bot credentials must be single-line values")
        allowed = frozenset(value.strip() for value in values.get("QQBOT_ALLOWED_PUBLICATIONS", "").split(",")
                            if value.strip())
        supported = {"group_update", "urgent_escalation"}
        if not allowed.issubset(supported):
            raise ValueError("QQBOT_ALLOWED_PUBLICATIONS supports group_update and urgent_escalation")
        api_base_url = (values.get("QQBOT_API_BASE_URL") or "https://api.bot.qq.com").strip().rstrip("/")
        parsed = urlsplit(api_base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("QQBOT_API_BASE_URL must be an HTTPS base URL without credentials or query")
        auto_execute = frozenset(value.strip() for value in values.get("QQBOT_AUTO_EXECUTE_CAPABILITIES", "").split(",")
                                 if value.strip())
        if not auto_execute.issubset({"dsh.task"}):
            raise ValueError("QQBOT_AUTO_EXECUTE_CAPABILITIES currently supports only dsh.task")
        workspace_id = values.get("COMPANY_AGENT_QQ_WORKSPACE", "").strip() or None
        if auto_execute and not workspace_id:
            raise ValueError("COMPANY_AGENT_QQ_WORKSPACE is required when QQ task execution is enabled")
        if workspace_id and (len(workspace_id) > 64 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", workspace_id)):
            raise ValueError("COMPANY_AGENT_QQ_WORKSPACE must be a 1-64 character workspace ID")
        isolate_workspaces = values.get("COMPANY_AGENT_QQ_ISOLATE_WORKSPACES", "true").strip().lower() in {
            "1", "true", "yes", "on"
        }
        return cls(app_id, client_secret, allowed, api_base_url, auto_execute, workspace_id,
                   isolate_workspaces=isolate_workspaces)


@dataclass(frozen=True)
class RuntimeConfig:
    model_url: str
    model_name: str
    api_key: str = field(repr=False)
    dsh_command: tuple[str, ...] = ()
    source_path: Path | None = None
    web_search_enabled: bool = False
    dsh_default_timeout_seconds: float = 3600.0
    dsh_max_timeout_seconds: float = 7200.0
    dsh_idle_timeout_seconds: float = 600.0
    dsh_compaction_timeout_seconds: float = 600.0
    dsh_soft_wrapup_seconds: float = 300.0
    web_gate_search_url: str | None = None
    web_gate_api_key: str = field(default="", repr=False)
    mcp_servers: tuple[McpServerSpec, ...] = ()
    mcp_secrets: dict[str, str] = field(default_factory=dict, repr=False)
    forge_mcp_enabled: bool = True
    forge_mcp_host: str = "127.0.0.1"
    forge_mcp_port: int = DEFAULT_FORGE_MCP_PORT
    forge_mcp_owner: str = "admin"

    @staticmethod
    def _parse_web_gate_search_url(values: dict[str, str]) -> str | None:
        has_gate_cfg = any(
            (values.get(key) or "").strip()
            for key in (
                "COMPANY_AGENT_WEB_GATE_URL",
                "COMPANY_AGENT_WEB_GATE_PORT",
                "COMPANY_AGENT_WEB_GATE_HOST",
                "COMPANY_AGENT_WEB_SEARCH_URL",
                "COMPANY_AGENT_SEARCH_PORT",
                "COMPANY_AGENT_PORT_BASE",
            )
        )
        if not has_gate_cfg:
            return None
        return resolve_port_settings(values).web_gate_dsh_search_url

    @classmethod
    def load(cls, path: Path) -> "RuntimeConfig":
        if not path.is_file():
            raise ValueError(f"runtime configuration not found: {path}")
        values = cls._parse_env(path)
        url = (values.get("AGENT_MODEL_URL") or values.get("DEEPSEEK_BASE_URL") or "").strip().rstrip("/")
        key = (values.get("AGENT_MODEL_API_KEY") or values.get("DEEPSEEK_API_KEY") or "").strip()
        model = (values.get("AGENT_MODEL_NAME") or "").strip()
        if not url or not key or not model:
            raise ValueError("AGENT_MODEL_URL, AGENT_MODEL_API_KEY and AGENT_MODEL_NAME are required")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("model URL must be an HTTP(S) base URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("model URL must not contain a query or fragment")
        if url.endswith("/chat/completions"):
            url = url.removesuffix("/chat/completions")
        if any(character in key + model for character in "\r\n\x00"):
            raise ValueError("model name and API key must be single-line values")
        command: tuple[str, ...] = ()
        if values.get("AGENT_DSH_COMMAND"):
            try:
                decoded = json.loads(values["AGENT_DSH_COMMAND"])
            except json.JSONDecodeError as error:
                raise ValueError("AGENT_DSH_COMMAND must be a JSON argument array") from error
            if not isinstance(decoded, list) or not decoded or any(not isinstance(arg, str) or not arg for arg in decoded):
                raise ValueError("AGENT_DSH_COMMAND must be a non-empty JSON string array")
            command = tuple(decoded)
        web_enabled = values.get("AGENT_WEB_SEARCH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        max_timeout = cls._parse_timeout(values, "AGENT_DSH_MAX_TIMEOUT_SECONDS", 7200.0, minimum=1.0, maximum=86400.0)
        default_timeout = cls._parse_timeout(values, "AGENT_DSH_TIMEOUT_SECONDS", min(3600.0, max_timeout),
                                             minimum=1.0, maximum=max_timeout)
        idle_timeout = cls._parse_timeout(values, "AGENT_DSH_IDLE_TIMEOUT_SECONDS", min(600.0, max_timeout),
                                          minimum=1.0, maximum=max_timeout)
        compaction_timeout = cls._parse_timeout(values, "AGENT_DSH_COMPACTION_TIMEOUT_SECONDS", min(600.0, max_timeout),
                                                minimum=1.0, maximum=max_timeout)
        soft_wrapup = cls._parse_timeout(values, "AGENT_DSH_SOFT_WRAPUP_SECONDS", min(300.0, max_timeout),
                                         minimum=0.0, maximum=min(600.0, max_timeout), allow_zero=True)
        web_gate_search_url = cls._parse_web_gate_search_url(values)
        web_gate_api_key = (values.get("COMPANY_AGENT_WEB_GATE_API_KEY") or "").strip()
        if any(character in web_gate_api_key for character in "\r\n\x00"):
            raise ValueError("web gate API key must be a single-line value")
        mcp_servers = parse_mcp_servers_json(values.get("COMPANY_AGENT_MCP_SERVERS", ""))
        mcp_secrets = load_mcp_secrets(values, mcp_servers)
        forge_enabled, forge_host, forge_port, forge_owner = parse_forge_mcp_settings(values)
        return cls(
            url, model, key, command, path.expanduser().resolve(), web_enabled,
            dsh_default_timeout_seconds=default_timeout,
            dsh_max_timeout_seconds=max_timeout,
            dsh_idle_timeout_seconds=idle_timeout,
            dsh_compaction_timeout_seconds=compaction_timeout,
            dsh_soft_wrapup_seconds=soft_wrapup,
            web_gate_search_url=web_gate_search_url,
            web_gate_api_key=web_gate_api_key,
            mcp_servers=mcp_servers,
            mcp_secrets=mcp_secrets,
            forge_mcp_enabled=forge_enabled,
            forge_mcp_host=forge_host,
            forge_mcp_port=forge_port,
            forge_mcp_owner=forge_owner,
        )

    @staticmethod
    def _parse_timeout(
        values: dict[str, str],
        key: str,
        default: float,
        *,
        minimum: float,
        maximum: float,
        allow_zero: bool = False,
    ) -> float:
        raw = values.get(key, "").strip()
        if not raw:
            return float(default)
        try:
            parsed = float(raw)
        except ValueError as error:
            raise ValueError(f"{key} must be a valid number") from error
        lower_ok = parsed >= minimum if allow_zero else parsed > 0 and parsed >= minimum
        if not math.isfinite(parsed) or not lower_ok or parsed > maximum:
            raise ValueError(f"{key} must be between {minimum:g} and {maximum:g}")
        return parsed

    @staticmethod
    def _parse_env(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                raise ValueError(f"invalid runtime configuration at line {line_number}")
            name, raw_value = line.split("=", 1)
            name = name.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid runtime configuration key at line {line_number}")
            value = raw_value.strip()
            if value.startswith(("'", '"')):
                quote = value[0]
                escaped = False
                end = None
                for index, character in enumerate(value[1:], start=1):
                    if character == quote and (quote == "'" or not escaped):
                        end = index
                        break
                    if character == "\\" and quote == '"' and not escaped:
                        escaped = True
                    else:
                        escaped = False
                if end is None or value[end + 1:].strip() and not value[end + 1:].lstrip().startswith("#"):
                    raise ValueError(f"invalid quoted runtime value at line {line_number}")
                value = value[1:end]
            else:
                value = re.sub(r"\s+#.*$", "", value).rstrip()
            values[name] = value
        return values

    def provider_patch(self, *, web_enabled: bool | None = None) -> list[dict[str, object]]:
        enabled = self.web_search_enabled if web_enabled is None else web_enabled
        return [
            {"id": "agent-default-model", "config": {"provider": "company-agent", "model": self.model_name}},
            {"id": "llm-pi-ai", "config": {"providers": {"company-agent": {
                "apiKeyEnv": "COMPANY_AGENT_MODEL_API_KEY",
                "api": "openai-completions",
                "baseURL": self.model_url,
                "compat": {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"},
                "models": [{"id": self.model_name, "contextWindow": 128000, "maxTokens": 4096}],
            }}}},
            {"id": "llm-deepseek", "disabled": True},
            {"id": "tool-web", "disabled": not enabled},
            {"id": "plugin-manager", "disabled": True},
            {"id": "tool-plugin-manager", "disabled": True},
        ]

    def resolve_command(self) -> tuple[str, ...]:
        if self.dsh_command:
            return self.dsh_command
        installed = shutil.which("dsh")
        if installed:
            return (installed,)
        node = shutil.which("node")
        if not node:
            nodes = sorted(Path.home().glob("node-v24.*-linux-x64/bin/node"))
            node = str(nodes[-1]) if nodes else None
        if node:
            candidates = sorted(Path.home().glob(".npm-company-agent/_npx/*/node_modules/@deepseek-ai/dsh/package.json"))
            for manifest in candidates:
                if json.loads(manifest.read_text(encoding="utf-8")).get("version") == DSH_VERSION:
                    return (node, str(manifest.parent / "lib" / "bin.js"))
        raise ValueError(f"DSH {DSH_VERSION} is not installed; see docs/04-step3-dsh.md")

    def environment(self, *, home: Path, dsh_home: Path, temporary_directory: Path) -> dict[str, str]:
        environment = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "SystemRoot", "WINDIR")
                       if name in os.environ}
        command = self.resolve_command()
        node_directory = str(Path(command[0]).resolve().parent)
        environment.update({
            "PATH": node_directory + os.pathsep + environment.get("PATH", os.defpath),
            "HOME": str(home), "USERPROFILE": str(home),
            "TMPDIR": str(temporary_directory), "TMP": str(temporary_directory), "TEMP": str(temporary_directory),
            "DSH_HOME": str(dsh_home), "DSH_TELEMETRY_DISABLED": "1",
            "COMPANY_AGENT_MODEL_API_KEY": self.api_key,
        })
        return environment

    def redact(self, text: str) -> str:
        secrets = sorted(
            {s for s in (self.api_key, self.web_gate_api_key, *self.mcp_secrets.values()) if s},
            key=len,
            reverse=True,
        )
        for secret in secrets:
            for value in (secret, json.dumps(secret)[1:-1]):
                text = text.replace(value, "[REDACTED]")
        return text
