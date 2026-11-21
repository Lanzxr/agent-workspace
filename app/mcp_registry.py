from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

MCP_ACCESS_LEVELS = ("admin", "internal", "common")
MCP_TRANSPORTS = ("stdio", "streamable-http")

_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TEMPLATE_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_INLINE_SECRET_KEY_RE = re.compile(r"(?:KEY|PASSWORD|SECRET|TOKEN|AUTH|COOKIE|CREDENTIAL)", re.IGNORECASE)
_LOCAL_BRIDGE_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "wsl-host", "host.wsl.internal", "host.docker.internal"})

# Host platform credentials that an MCP child or HTTP header must never borrow.
_RESERVED_PLATFORM_ENV_VARS = frozenset({
    "AGENT_MODEL_API_KEY",
    "DEEPSEEK_API_KEY",
    "COMPANY_AGENT_MODEL_API_KEY",
    "COMPANY_AGENT_ADMIN_TOKEN",
    "COMPANY_AGENT_KNOWLEDGE_API_KEY",
    "COMPANY_AGENT_KNOWLEDGE_GRANT_SECRET",
    "COMPANY_AGENT_WEB_GATE_API_KEY",
    "ANYTHINGLLM_API_KEY",
    "QQBOT_CLIENT_SECRET",
    "QQBOT_SECRET",
    "SEARXNG_SECRET",
})

_DEFAULT_TIMEOUT_MS = 60_000
_MIN_TIMEOUT_MS = 1_000
_MAX_TIMEOUT_MS = 300_000


class McpConfigError(ValueError):
    """Raised when a controlled MCP server specification or secret reference is invalid."""


def normalize_workspace_tier(workspace_tier: str | None) -> str:
    value = str(workspace_tier or "").strip().lower()
    if value in {"admin", "internal", "client_group", "direct"}:
        return value
    return "client_group"


def tier_allows_mcp(access_level: str, workspace_tier: str | None) -> bool:
    """Return True when the session tier is permitted to mount an MCP server of access_level."""
    tier = normalize_workspace_tier(workspace_tier)
    if access_level == "common":
        return tier in {"admin", "internal", "client_group", "direct"}
    if access_level == "internal":
        return tier in {"admin", "internal"}
    if access_level == "admin":
        return tier == "admin"
    return False


def _validate_single_line(value: Any, *, field_name: str, allow_empty: bool = False, max_len: int = 2000) -> str:
    if not isinstance(value, str):
        raise McpConfigError(f"{field_name} must be a string")
    if any(ch in value for ch in "\r\n\x00"):
        raise McpConfigError(f"{field_name} must be single-line without control characters")
    stripped = value.strip()
    if not allow_empty and not stripped:
        raise McpConfigError(f"{field_name} must not be empty")
    if len(stripped) > max_len:
        raise McpConfigError(f"{field_name} exceeds {max_len} characters")
    return stripped


def _validate_env_name(name: Any, *, field_name: str, check_reserved: bool = False) -> str:
    text = _validate_single_line(name, field_name=field_name, max_len=128)
    if not _ENV_NAME_RE.fullmatch(text):
        raise McpConfigError(f"{field_name} must be a valid environment variable name: {text!r}")
    if check_reserved and text.upper() in _RESERVED_PLATFORM_ENV_VARS:
        raise McpConfigError(f"{field_name} cannot reference reserved platform credential {text}")
    return text


def _validate_non_secret_map(raw: Any, *, field_name: str) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise McpConfigError(f"{field_name} must be a JSON object")
    items: list[tuple[str, str]] = []
    for k, v in raw.items():
        key = _validate_single_line(k, field_name=f"{field_name} key", max_len=128)
        if _INLINE_SECRET_KEY_RE.search(key):
            raise McpConfigError(
                f"{field_name} key {key!r} looks like a credential; use env_refs/header_env_refs instead of inline literals"
            )
        val = _validate_single_line(v, field_name=f"{field_name}[{key}]", allow_empty=True, max_len=1000)
        items.append((key, val))
    items.sort(key=lambda pair: pair[0])
    return tuple(items)


def _validate_env_refs(raw: Any) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise McpConfigError("env_refs must be a JSON object")
    pairs: list[tuple[str, str]] = []
    for target, host_var in raw.items():
        t_name = _validate_env_name(target, field_name="env_refs target")
        h_name = _validate_env_name(host_var, field_name=f"env_refs[{t_name}]", check_reserved=True)
        pairs.append((t_name, h_name))
    pairs.sort(key=lambda pair: pair[0])
    return tuple(pairs)


def _validate_header_env_refs(raw: Any) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise McpConfigError("header_env_refs must be a JSON object")
    pairs: list[tuple[str, str]] = []
    for header, template in raw.items():
        h_name = _validate_single_line(header, field_name="header_env_refs header", max_len=128)
        tpl = _validate_single_line(template, field_name=f"header_env_refs[{h_name}]", max_len=512)
        if _ENV_NAME_RE.fullmatch(tpl):
            tpl = f"${{{tpl}}}"
        refs = _TEMPLATE_REF_RE.findall(tpl)
        if not refs:
            raise McpConfigError(
                f"header_env_refs[{h_name}] must reference an environment variable as ${{VAR_NAME}}"
            )
        for ref in refs:
            _validate_env_name(ref, field_name=f"header_env_refs[{h_name}]", check_reserved=True)
        pairs.append((h_name, tpl))
    pairs.sort(key=lambda pair: pair[0])
    return tuple(pairs)


def _is_allowed_plaintext_http_host(hostname: str) -> bool:
    host = hostname.strip("[]").lower()
    if host in _LOCAL_BRIDGE_HOSTS:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if addr.is_unspecified or addr.is_link_local or addr.is_multicast:
        return False
    return bool(addr.is_loopback or addr.is_private)


def _validate_streamable_url(raw_url: Any) -> str:
    url = _validate_single_line(raw_url, field_name="url", max_len=1000)
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise McpConfigError("MCP streamable-http url must be HTTP(S) without embedded credentials or fragment")
    if parsed.scheme == "http" and not _is_allowed_plaintext_http_host(parsed.hostname):
        raise McpConfigError(
            "Plaintext http:// MCP url is restricted to loopback, wsl-host, or private host-bridge IPs"
        )
    return url


@dataclass(frozen=True)
class McpServerSpec:
    """Declarative non-secret definition of one controlled MCP server."""

    server_id: str
    description: str = ""
    transport: str = "stdio"
    access_level: str = "admin"
    enabled: bool = True
    command: str = ""
    args: tuple[str, ...] = ()
    cwd: str = ""
    env: tuple[tuple[str, str], ...] = ()
    env_refs: tuple[tuple[str, str], ...] = ()
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    header_env_refs: tuple[tuple[str, str], ...] = ()
    tool_call_timeout_ms: int = _DEFAULT_TIMEOUT_MS
    fail_on_startup_error: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, str) or not _SERVER_ID_RE.fullmatch(self.server_id):
            raise McpConfigError("server_id must match ^[A-Za-z0-9_-]{1,32}$")
        if self.transport not in MCP_TRANSPORTS:
            raise McpConfigError(f"transport must be one of {MCP_TRANSPORTS}")
        if self.access_level not in MCP_ACCESS_LEVELS:
            raise McpConfigError(f"access_level must be one of {MCP_ACCESS_LEVELS}")
        if not isinstance(self.enabled, bool):
            raise McpConfigError("enabled must be a boolean")
        if not isinstance(self.fail_on_startup_error, bool):
            raise McpConfigError("fail_on_startup_error must be a boolean")
        if (
            isinstance(self.tool_call_timeout_ms, bool)
            or not isinstance(self.tool_call_timeout_ms, int)
            or not _MIN_TIMEOUT_MS <= self.tool_call_timeout_ms <= _MAX_TIMEOUT_MS
        ):
            raise McpConfigError(f"tool_call_timeout_ms must be between {_MIN_TIMEOUT_MS} and {_MAX_TIMEOUT_MS}")
        if self.transport == "stdio":
            _validate_single_line(self.command, field_name="command", max_len=512)
        else:
            _validate_streamable_url(self.url)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "McpServerSpec":
        if not isinstance(data, Mapping):
            raise McpConfigError("MCP server definition must be an object")
        raw_id = data.get("server_id") if "server_id" in data else data.get("id")
        server_id = _validate_single_line(raw_id, field_name="server_id", max_len=32)
        description = _validate_single_line(
            data.get("description", ""), field_name="description", allow_empty=True, max_len=300
        )
        transport = _validate_single_line(data.get("transport", "stdio"), field_name="transport", max_len=32)
        access_level = _validate_single_line(
            data.get("access_level", "admin"), field_name="access_level", max_len=32
        ).lower()
        enabled_raw = data.get("enabled", True)
        if not isinstance(enabled_raw, bool):
            raise McpConfigError("enabled must be a boolean")
        fail_raw = data.get("fail_on_startup_error", False)
        if not isinstance(fail_raw, bool):
            raise McpConfigError("fail_on_startup_error must be a boolean")
        timeout_raw = data.get("tool_call_timeout_ms", _DEFAULT_TIMEOUT_MS)
        if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int):
            raise McpConfigError("tool_call_timeout_ms must be an integer")

        if transport == "stdio":
            command = _validate_single_line(data.get("command", ""), field_name="command", max_len=512)
            raw_args = data.get("args", [])
            if raw_args is None:
                raw_args = []
            if not isinstance(raw_args, Sequence) or isinstance(raw_args, (str, bytes)) or len(raw_args) > 32:
                raise McpConfigError("args must be an array of at most 32 strings")
            args = tuple(_validate_single_line(a, field_name="args item", allow_empty=True, max_len=512) for a in raw_args)
            cwd = _validate_single_line(data.get("cwd", ""), field_name="cwd", allow_empty=True, max_len=512)
            env = _validate_non_secret_map(data.get("env"), field_name="env")
            env_refs = _validate_env_refs(data.get("env_refs"))
            return cls(
                server_id=server_id,
                description=description,
                transport=transport,
                access_level=access_level,
                enabled=enabled_raw,
                command=command,
                args=args,
                cwd=cwd,
                env=env,
                env_refs=env_refs,
                tool_call_timeout_ms=timeout_raw,
                fail_on_startup_error=fail_raw,
            )

        if transport == "streamable-http":
            url = _validate_streamable_url(data.get("url", ""))
            headers = _validate_non_secret_map(data.get("headers"), field_name="headers")
            header_env_refs = _validate_header_env_refs(data.get("header_env_refs"))
            return cls(
                server_id=server_id,
                description=description,
                transport=transport,
                access_level=access_level,
                enabled=enabled_raw,
                url=url,
                headers=headers,
                header_env_refs=header_env_refs,
                tool_call_timeout_ms=timeout_raw,
                fail_on_startup_error=fail_raw,
            )

        raise McpConfigError(f"unsupported MCP transport: {transport!r}")

    def allows_tier(self, workspace_tier: str | None) -> bool:
        return self.enabled and tier_allows_mcp(self.access_level, workspace_tier)

    def referenced_host_env_vars(self) -> tuple[str, ...]:
        names: list[str] = []
        seen: set[str] = set()
        for _, host_var in self.env_refs:
            if host_var not in seen:
                seen.add(host_var)
                names.append(host_var)
        for _, tpl in self.header_env_refs:
            for ref in _TEMPLATE_REF_RE.findall(tpl):
                if ref not in seen:
                    seen.add(ref)
                    names.append(ref)
        return tuple(names)

    def to_summary_dict(self) -> dict[str, str]:
        """Minimal metadata safe for orchestrator planning without host paths, URLs, or credentials."""
        return {
            "id": self.server_id,
            "description": self.description,
            "access_level": self.access_level,
            "transport": self.transport,
            "tool_prefix": f"mcp__{self.server_id}__",
        }

    def to_dict(self, *, source: str = "runtime") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "server_id": self.server_id,
            "description": self.description,
            "transport": self.transport,
            "access_level": self.access_level,
            "enabled": self.enabled,
            "tool_call_timeout_ms": self.tool_call_timeout_ms,
            "fail_on_startup_error": self.fail_on_startup_error,
            "referenced_secret_vars": list(self.referenced_host_env_vars()),
            "source": source,
        }
        if self.transport == "stdio":
            payload.update({
                "command": self.command,
                "args": list(self.args),
                "cwd": self.cwd,
                "env": dict(self.env),
                "env_refs": dict(self.env_refs),
            })
        else:
            payload.update({
                "url": self.url,
                "headers": dict(self.headers),
                "header_env_refs": dict(self.header_env_refs),
            })
        return payload

    def to_plugin_config(self, *, mcp_client_module_path: str | None = None) -> dict[str, Any]:
        """Generate DSH provider patch config containing ONLY non-secret fields and env var references."""
        cfg: dict[str, Any] = {
            "serverName": self.server_id,
            "transport": self.transport,
            "toolCallTimeoutMs": self.tool_call_timeout_ms,
            "failOnStartupError": self.fail_on_startup_error,
        }
        if mcp_client_module_path:
            cfg["mcpClientModulePath"] = mcp_client_module_path
        if self.transport == "stdio":
            cfg.update({
                "command": self.command,
                "args": list(self.args),
                "cwd": self.cwd,
                "env": dict(self.env),
                "envRefs": dict(self.env_refs),
            })
        else:
            cfg.update({
                "url": self.url,
                "headers": dict(self.headers),
                "headerEnvRefs": dict(self.header_env_refs),
            })
        return cfg


def parse_mcp_servers_json(raw: str) -> tuple[McpServerSpec, ...]:
    text = (raw or "").strip()
    if not text:
        return ()
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as error:
        raise McpConfigError("COMPANY_AGENT_MCP_SERVERS must be valid JSON") from error
    if isinstance(decoded, Mapping) and isinstance(decoded.get("servers"), list):
        decoded = decoded["servers"]
    if not isinstance(decoded, list) or len(decoded) > 50:
        raise McpConfigError("COMPANY_AGENT_MCP_SERVERS must be a JSON array of at most 50 server specs")
    specs: list[McpServerSpec] = []
    seen: set[str] = set()
    for item in decoded:
        spec = McpServerSpec.from_dict(item)
        if spec.server_id in seen:
            raise McpConfigError(f"duplicate MCP server_id: {spec.server_id}")
        seen.add(spec.server_id)
        specs.append(spec)
    return tuple(specs)


def load_mcp_secrets(env_values: Mapping[str, str], servers: Sequence[McpServerSpec]) -> dict[str, str]:
    """Extract only referenced MCP secrets or COMPANY_AGENT_MCP_SECRET_* entries from runtime.env."""
    referenced: set[str] = set()
    for server in servers:
        referenced.update(server.referenced_host_env_vars())
    secrets: dict[str, str] = {}
    for key, raw_value in env_values.items():
        if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
            continue
        if key not in referenced and not key.startswith("COMPANY_AGENT_MCP_SECRET_"):
            continue
        if key.upper() in _RESERVED_PLATFORM_ENV_VARS:
            continue
        value = str(raw_value or "").strip()
        if not value:
            continue
        if any(ch in value for ch in "\r\n\x00"):
            raise McpConfigError(f"MCP secret {key} must be a single-line value")
        secrets[key] = value
    return secrets


def merge_mcp_servers(
    runtime_servers: Sequence[McpServerSpec],
    console_entries: Sequence[Mapping[str, Any]] | Any = (),
) -> tuple[McpServerSpec, ...]:
    """Merge runtime.env baseline servers with Control Plane non-secret overrides/entries."""
    merged: dict[str, McpServerSpec] = {spec.server_id: spec for spec in runtime_servers}
    order: list[str] = [spec.server_id for spec in runtime_servers]
    if isinstance(console_entries, Sequence) and not isinstance(console_entries, (str, bytes)):
        for raw in console_entries:
            if not isinstance(raw, Mapping):
                continue
            raw_id = str(raw.get("server_id") or raw.get("id") or "").strip()
            if not raw_id or not _SERVER_ID_RE.fullmatch(raw_id):
                continue
            base = merged.get(raw_id)
            if base is not None and "transport" not in raw:
                updated_dict = base.to_dict()
                for mutable_key in ("enabled", "access_level", "description", "tool_call_timeout_ms", "fail_on_startup_error"):
                    if mutable_key in raw:
                        updated_dict[mutable_key] = raw[mutable_key]
                spec = McpServerSpec.from_dict(updated_dict)
            else:
                spec = McpServerSpec.from_dict(raw)
                if raw_id not in merged:
                    order.append(raw_id)
            merged[raw_id] = spec
    return tuple(merged[sid] for sid in order if sid in merged)


def all_mcp_secret_env_names(
    servers: Sequence[McpServerSpec],
    mcp_secrets: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return every host credential env var name that must be stripped from sandbox bash processes."""
    names: set[str] = set()
    for server in servers:
        names.update(server.referenced_host_env_vars())
    if mcp_secrets:
        names.update(mcp_secrets.keys())
    return tuple(sorted(names))


def describe_mcp_servers(
    runtime_servers: Sequence[McpServerSpec],
    console_entries: Sequence[Mapping[str, Any]] | Any = (),
    mcp_secrets: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return non-secret server records with source & secret-availability indicators for Admin Console."""
    runtime_ids = {s.server_id for s in runtime_servers}
    console_ids = {
        str(item.get("server_id") or item.get("id") or "").strip()
        for item in (console_entries if isinstance(console_entries, Sequence) and not isinstance(console_entries, (str, bytes)) else ())
        if isinstance(item, Mapping)
    }
    secret_pool = mcp_secrets or {}
    merged = merge_mcp_servers(runtime_servers, console_entries)
    rows: list[dict[str, Any]] = []
    for spec in merged:
        if spec.server_id in runtime_ids:
            source = "runtime+override" if spec.server_id in console_ids else "runtime"
        else:
            source = "console"
        record = spec.to_dict(source=source)
        missing_vars = [
            var for var in spec.referenced_host_env_vars()
            if not (secret_pool.get(var) or os.environ.get(var))
        ]
        record["missing_secret_vars"] = missing_vars
        rows.append(record)
    return rows
