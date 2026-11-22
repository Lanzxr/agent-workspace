from __future__ import annotations

import json
import math
import os
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .mcp_registry import McpServerSpec, load_mcp_secrets, parse_mcp_servers_json
from .ports import DEFAULT_FORGE_MCP_PORT, resolve_port_settings


VALID_REASONING_EFFORTS = frozenset({"off", "low", "medium", "high"})


def mask_secret(value: str) -> str:
    """Return a non-reversible masked preview of a secret for UI display."""
    clean = (value or "").strip()
    if not clean:
        return ""
    if len(clean) >= 8:
        return f"••••{clean[-4:]}"
    return "••••"


def format_env_value(value: str) -> str:
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError("runtime configuration values must be single-line strings")
    if not value:
        return ""
    if any(ch.isspace() for ch in value) or any(ch in value for ch in ('#', '"', "'")):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def update_runtime_env_file(path: Path, updates: Mapping[str, str | None]) -> dict[str, str]:
    """Atomically update selected keys in a 0600 runtime.env file while preserving comments and order."""
    for key, val in updates.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid runtime configuration key: {key}")
        if val is not None and any(ch in str(val) for ch in "\r\n\x00"):
            raise ValueError(f"{key} must be a single-line value")

    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    raw_lines = resolved.read_text(encoding="utf-8-sig").splitlines() if resolved.is_file() else []

    remaining = dict(updates)
    out_lines: list[str] = []
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(raw_line)
            continue
        candidate = stripped[7:].lstrip() if stripped.startswith("export ") else stripped
        if "=" not in candidate:
            out_lines.append(raw_line)
            continue
        name = candidate.split("=", 1)[0].strip()
        if name not in remaining:
            out_lines.append(raw_line)
            continue
        new_val = remaining.pop(name)
        if new_val is None:
            continue
        prefix = "export " if stripped.startswith("export ") else ""
        out_lines.append(f"{prefix}{name}={format_env_value(str(new_val))}")

    for name, new_val in remaining.items():
        if new_val is None:
            continue
        out_lines.append(f"{name}={format_env_value(str(new_val))}")

    content = "\n".join(out_lines) + ("\n" if out_lines else "")
    tmp_path = resolved.parent / f".{resolved.name}.{os.getpid()}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, resolved)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return RuntimeConfig._parse_env(resolved)


def parse_forge_mcp_settings(values: Mapping[str, str]) -> tuple[bool, str, int, str]:
    """Parse shared Tool Forge loopback MCP server settings with strict validation."""
    enabled = str(values.get("COMPANY_AGENT_FORGE_MCP_ENABLED") or "true").strip().lower() in {"1", "true", "yes", "on"}
    ports = resolve_port_settings(values)
    host = ports.forge_mcp_host
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("COMPANY_AGENT_FORGE_MCP_HOST must bind to 127.0.0.1 or localhost")
    owner = str(values.get("COMPANY_AGENT_FORGE_MCP_OWNER") or "admin").strip().lower() or "admin"
    if owner not in {"admin", "qq", "wecom"}:
        raise ValueError("COMPANY_AGENT_FORGE_MCP_OWNER must be admin, qq, or wecom")
    return enabled, host, ports.forge_mcp_port, owner

DSH_VERSION = "0.2.2-rc.1"
POLICY_VERSION = "workspace-dev-v1"
DEFAULT_MODEL_CONTEXT_WINDOW = 1048576
DEFAULT_MODEL_MAX_TOKENS = 393216


@dataclass(frozen=True)
class QQBotSettings:
    app_id: str
    client_secret: str = field(repr=False)
    allowed_publications: frozenset[str] = frozenset()
    api_base_url: str = "https://api.bot.qq.com"
    auto_execute_capabilities: frozenset[str] = frozenset()
    workspace_id: str | None = None
    isolate_workspaces: bool = True

    @property
    def channel_name(self) -> str:
        return "qq"

    @property
    def account_id(self) -> str:
        return self.app_id

    @classmethod
    def is_configured(cls, path: Path) -> bool:
        if not path.is_file():
            return False
        values = RuntimeConfig._parse_env(path)
        app_id = (values.get("QQBOT_APP_ID") or values.get("QQBOT_APPID") or "").strip()
        secret = (values.get("QQBOT_CLIENT_SECRET") or values.get("QQBOT_SECRET") or "").strip()
        return bool(app_id and secret)

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


WECOM_DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"


@dataclass(frozen=True)
class WeComBotSettings:
    bot_id: str
    bot_secret: str = field(repr=False)
    allowed_publications: frozenset[str] = frozenset()
    ws_url: str = WECOM_DEFAULT_WS_URL
    auto_execute_capabilities: frozenset[str] = frozenset()
    workspace_id: str | None = None
    isolate_workspaces: bool = True
    group_direct_on_callback: bool = True

    @property
    def channel_name(self) -> str:
        return "wecom"

    @property
    def account_id(self) -> str:
        return self.bot_id

    @classmethod
    def is_configured(cls, path: Path) -> bool:
        if not path.is_file():
            return False
        values = RuntimeConfig._parse_env(path)
        bot_id = (values.get("WECOM_BOT_ID") or values.get("COMPANY_AGENT_WECOM_BOT_ID") or "").strip()
        secret = (values.get("WECOM_BOT_SECRET") or values.get("COMPANY_AGENT_WECOM_BOT_SECRET") or "").strip()
        return bool(bot_id and secret)

    @classmethod
    def load(cls, path: Path) -> "WeComBotSettings":
        if not path.is_file():
            raise ValueError(f"runtime configuration not found: {path}")
        values = RuntimeConfig._parse_env(path)
        bot_id = (values.get("WECOM_BOT_ID") or values.get("COMPANY_AGENT_WECOM_BOT_ID") or "").strip()
        bot_secret = (values.get("WECOM_BOT_SECRET") or values.get("COMPANY_AGENT_WECOM_BOT_SECRET") or "").strip()
        if not bot_id or not bot_secret:
            raise ValueError("WECOM_BOT_ID and WECOM_BOT_SECRET are required")
        if any(character in bot_id + bot_secret for character in "\r\n\x00"):
            raise ValueError("WeCom bot credentials must be single-line values")
        allowed = frozenset(
            value.strip() for value in values.get("WECOM_ALLOWED_PUBLICATIONS", "").split(",") if value.strip()
        )
        supported = {"group_update", "urgent_escalation"}
        if not allowed.issubset(supported):
            raise ValueError("WECOM_ALLOWED_PUBLICATIONS supports group_update and urgent_escalation")
        ws_url = (values.get("WECOM_WS_URL") or WECOM_DEFAULT_WS_URL).strip()
        parsed = urlsplit(ws_url)
        if (
            parsed.scheme != "wss"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("WECOM_WS_URL must be a wss:// URL without embedded credentials or fragment")
        auto_execute = frozenset(
            value.strip()
            for value in values.get("WECOM_AUTO_EXECUTE_CAPABILITIES", "").split(",")
            if value.strip()
        )
        if not auto_execute.issubset({"dsh.task"}):
            raise ValueError("WECOM_AUTO_EXECUTE_CAPABILITIES currently supports only dsh.task")
        workspace_id = values.get("COMPANY_AGENT_WECOM_WORKSPACE", "").strip() or None
        if auto_execute and not workspace_id:
            raise ValueError("COMPANY_AGENT_WECOM_WORKSPACE is required when WeCom task execution is enabled")
        if workspace_id and (len(workspace_id) > 64 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", workspace_id)):
            raise ValueError("COMPANY_AGENT_WECOM_WORKSPACE must be a 1-64 character workspace ID")
        isolate_workspaces = values.get("COMPANY_AGENT_WECOM_ISOLATE_WORKSPACES", "true").strip().lower() in {
            "1", "true", "yes", "on",
        }
        group_direct_on_callback = values.get("WECOM_GROUP_DIRECT_ON_CALLBACK", "true").strip().lower() in {
            "1", "true", "yes", "on",
        }
        return cls(
            bot_id=bot_id,
            bot_secret=bot_secret,
            allowed_publications=allowed,
            ws_url=ws_url,
            auto_execute_capabilities=auto_execute,
            workspace_id=workspace_id,
            isolate_workspaces=isolate_workspaces,
            group_direct_on_callback=group_direct_on_callback,
        )


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
    model_context_window: int = DEFAULT_MODEL_CONTEXT_WINDOW
    model_max_tokens: int = DEFAULT_MODEL_MAX_TOKENS
    model_reasoning_effort: str | None = None
    fallback_model_name: str | None = None
    fallback_model_url: str | None = None
    fallback_api_key: str = field(default="", repr=False)

    @property
    def has_fallback_model(self) -> bool:
        if not self.fallback_model_name:
            return False
        fb_url = self.fallback_model_url or self.model_url
        return self.fallback_model_name != self.model_name or fb_url != self.model_url

    def with_fallback_as_primary(self) -> "RuntimeConfig":
        if not self.fallback_model_name:
            raise ValueError("fallback model is not configured")
        return replace(
            self,
            model_name=self.fallback_model_name,
            model_url=self.fallback_model_url or self.model_url,
            api_key=self.fallback_api_key or self.api_key,
            fallback_model_name=None,
            fallback_model_url=None,
            fallback_api_key="",
        )

    @staticmethod
    def _validate_model_url(raw_url: str, *, label: str = "model URL") -> str:
        url = raw_url.strip().rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"{label} must be an HTTP(S) base URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError(f"{label} must not contain a query or fragment")
        if url.endswith("/chat/completions"):
            url = url.removesuffix("/chat/completions")
        return url

    @staticmethod
    def _parse_int_limit(values: dict[str, str], key: str, default: int, *, minimum: int, maximum: int) -> int:
        raw = (values.get(key) or "").strip()
        if not raw:
            return int(default)
        try:
            parsed = int(raw)
        except ValueError as error:
            raise ValueError(f"{key} must be an integer") from error
        if not minimum <= parsed <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        return parsed

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
        url_raw = (values.get("AGENT_MODEL_URL") or values.get("DEEPSEEK_BASE_URL") or "").strip()
        key = (values.get("AGENT_MODEL_API_KEY") or values.get("DEEPSEEK_API_KEY") or "").strip()
        model = (values.get("AGENT_MODEL_NAME") or "").strip()
        if not url_raw or not key or not model:
            raise ValueError("AGENT_MODEL_URL, AGENT_MODEL_API_KEY and AGENT_MODEL_NAME are required")
        url = cls._validate_model_url(url_raw, label="model URL")
        if any(character in key + model for character in "\r\n\x00"):
            raise ValueError("model name and API key must be single-line values")
        context_window = cls._parse_int_limit(
            values, "AGENT_MODEL_CONTEXT_WINDOW", DEFAULT_MODEL_CONTEXT_WINDOW,
            minimum=4096, maximum=4_194_304,
        )
        max_tokens = cls._parse_int_limit(
            values, "AGENT_MODEL_MAX_TOKENS", min(DEFAULT_MODEL_MAX_TOKENS, context_window),
            minimum=512, maximum=min(2_097_152, context_window),
        )
        reasoning_raw = (values.get("AGENT_MODEL_REASONING_EFFORT") or "").strip().lower()
        reasoning_effort: str | None = None
        if reasoning_raw:
            if reasoning_raw not in VALID_REASONING_EFFORTS:
                raise ValueError("AGENT_MODEL_REASONING_EFFORT must be one of: off, low, medium, high")
            reasoning_effort = reasoning_raw
        fallback_name = (values.get("AGENT_MODEL_FALLBACK_NAME") or "").strip() or None
        fallback_url_raw = (values.get("AGENT_MODEL_FALLBACK_URL") or "").strip()
        fallback_key = (values.get("AGENT_MODEL_FALLBACK_API_KEY") or "").strip()
        if any(character in (fallback_name or "") + fallback_key for character in "\r\n\x00"):
            raise ValueError("fallback model name and API key must be single-line values")
        fallback_url: str | None = None
        if fallback_url_raw:
            fallback_url = cls._validate_model_url(fallback_url_raw, label="fallback model URL")
        elif fallback_name:
            fallback_url = url
        if fallback_name and not fallback_key:
            fallback_key = key
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
            model_context_window=context_window,
            model_max_tokens=max_tokens,
            model_reasoning_effort=reasoning_effort,
            fallback_model_name=fallback_name,
            fallback_model_url=fallback_url,
            fallback_api_key=fallback_key,
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
        default_model_cfg: dict[str, object] = {"provider": "company-agent", "model": self.model_name}
        compat_cfg: dict[str, object] = {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"}
        model_entry: dict[str, object] = {
            "id": self.model_name,
            "contextWindow": int(self.model_context_window),
            "maxTokens": int(self.model_max_tokens),
        }
        if self.model_reasoning_effort in VALID_REASONING_EFFORTS:
            default_model_cfg["reasoningEffort"] = self.model_reasoning_effort
            compat_cfg["supportsReasoningEffort"] = True
            model_entry["reasoningEfforts"] = {
                "off": "",
                "low": "low",
                "medium": "medium",
                "high": "high",
            }
        return [
            {"id": "agent-default-model", "config": default_model_cfg},
            {"id": "llm-pi-ai", "config": {"providers": {"company-agent": {
                "apiKeyEnv": "COMPANY_AGENT_MODEL_API_KEY",
                "api": "openai-completions",
                "baseURL": self.model_url,
                "compat": compat_cfg,
                "models": [model_entry],
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
            {s for s in (self.api_key, self.fallback_api_key, self.web_gate_api_key, *self.mcp_secrets.values()) if s},
            key=len,
            reverse=True,
        )
        for secret in secrets:
            for value in (secret, json.dumps(secret)[1:-1]):
                text = text.replace(value, "[REDACTED]")
        return text
