from __future__ import annotations

import ast
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from .control_plane import ControlPlane
from .mcp_registry import (
    MCP_ACCESS_LEVELS,
    _ENV_NAME_RE,
    _RESERVED_PLATFORM_ENV_VARS,
    McpServerSpec,
)
from .ports import DEFAULT_FORGE_MCP_PORT
from .shared_tools import SCRIPT_NAME_PATTERN, SharedToolsManager

_TEMPLATE_REF_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}")
_ARG_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*(?:args\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\}\}|\{(?:args\.)?([A-Za-z_][A-Za-z0-9_]*)\}"
)
_HMAC_CALL_SECRET_RE = re.compile(r"""hmac_sha256\(\s*['"]([A-Za-z_][A-Za-z0-9_]{0,63})['"]""")
_TUN_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
FORGE_TOOL_KINDS = ("http_api", "python_plugin", "shared_script")
FORGE_OPERATION_TYPES = ("read", "mutating", "write")
DEFAULT_FORGE_MCP_HOST = "127.0.0.1"
MAX_PLUGIN_CODE_CHARS = 64_000
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESULT_CHARS = 200_000
PLUGIN_TIMEOUT_SECONDS = 25.0

_ALLOWED_PLUGIN_MODULES = frozenset({
    "base64",
    "collections",
    "csv",
    "datetime",
    "decimal",
    "functools",
    "hashlib",
    "hmac",
    "io",
    "itertools",
    "json",
    "math",
    "re",
    "statistics",
    "typing",
    "urllib",
    "urllib.parse",
})

_BLOCKED_PLUGIN_NAMES = frozenset({
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "getattr",
    "globals",
    "input",
    "locals",
    "memoryview",
    "open",
    "setattr",
    "super",
    "type",
    "vars",
})

_INLINE_SECRET_HEADER_RE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._~+/=-]{12,}|Basic\s+[A-Za-z0-9+/=]{12,}|"
    r"(?:api[_-]?key|token|secret|password|cookie)\s*[:=]\s*[^\s,;\"']{8,})",
    re.IGNORECASE,
)


class ToolForgeError(ValueError):
    """Base error for controlled Tool Forge validation, persistence, and execution."""


class ToolForgeSecurityError(ToolForgeError):
    """Raised when a URL, plugin AST, secret reference, or permission tier violates security policy."""


class ToolForgeValidationError(ToolForgeError):
    """Raised when schema, metadata, or test prerequisites fail."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_domain(raw: str) -> str:
    text = str(raw or "").strip().lower().rstrip(".")
    if not text or any(ch in text for ch in "/:@?#\r\n\x00 "):
        raise ToolForgeValidationError("allowed domain must be a bare hostname or IP without scheme or path")
    candidate = text.strip("[]")
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        if not DOMAIN_RE.fullmatch(text):
            raise ToolForgeValidationError(f"invalid hostname: {text!r}")
        return text
    if ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast:
        raise ToolForgeSecurityError(f"forbidden reserved IP address for allowlist: {candidate}")
    return str(ip)


def validate_secret_name(name: Any) -> str:
    text = str(name or "").strip()
    if not text or not _ENV_NAME_RE.fullmatch(text) or len(text) > 128:
        raise ToolForgeValidationError(f"invalid secret alias name: {name!r}")
    if text.upper() in _RESERVED_PLATFORM_ENV_VARS or text.upper().startswith("COMPANY_AGENT_MCP_SECRET_FORGE_"):
        raise ToolForgeSecurityError(f"cannot overwrite or reference reserved platform secret {text}")
    return text


def redact_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    cleaned = str(text or "")
    for secret in sorted({s for s in extra_secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True):
        if secret in cleaned:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    cleaned = _INLINE_SECRET_HEADER_RE.sub("[REDACTED_CREDENTIAL]", cleaned)
    return cleaned


def redact_value(value: Any, extra_secrets: Iterable[str] = ()) -> Any:
    if isinstance(value, str):
        return redact_text(value, extra_secrets)
    if isinstance(value, list):
        return [redact_value(item, extra_secrets) for item in value]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            lowered = key_str.lower()
            if (
                lowered in {"cookie", "set-cookie", "authorization", "password", "secret", "totp_secret", "admin_cookie"}
                or (lowered.endswith(("_token", "_secret", "_password", "_cookie")) and "prompt" not in lowered and "cached" not in lowered)
            ):
                sanitized[key_str] = "[REDACTED]"
            else:
                sanitized[key_str] = redact_value(v, extra_secrets)
        return sanitized
    return value


class ForgeSecretsVault:
    """File-backed 0600 secret storage isolated from SQLite, DSH workspaces, and LLM context."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()
        self._lock = threading.RLock()

    def _read_raw(self) -> dict[str, dict[str, str]]:
        if self.path.is_symlink():
            raise ToolForgeSecurityError("secrets vault cannot be a symlink")
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ToolForgeError("corrupt forge secrets vault") from error
        if not isinstance(payload, dict):
            return {}
        entries = payload.get("secrets")
        if not isinstance(entries, dict):
            return {}
        cleaned: dict[str, dict[str, str]] = {}
        for name, item in entries.items():
            if isinstance(name, str) and isinstance(item, dict) and isinstance(item.get("value"), str):
                cleaned[name] = {"value": item["value"], "updated_at": str(item.get("updated_at") or "")}
        return cleaned

    def _write_raw(self, entries: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps({"version": 1, "secrets": entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            tmp.chmod(0o600)
        tmp.replace(self.path)
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def list_metadata(self) -> list[dict[str, Any]]:
        with self._lock:
            entries = self._read_raw()
            return [
                {"name": name, "configured": bool(item["value"]), "updated_at": item["updated_at"]}
                for name, item in sorted(entries.items())
            ]

    def set_secret(self, name: str, value: str) -> dict[str, Any]:
        validated_name = validate_secret_name(name)
        if not isinstance(value, str) or not value.strip() or any(ch in value for ch in "\r\n\x00"):
            raise ToolForgeValidationError("secret value must be a non-empty single-line string")
        cleaned_val = value.strip()
        if len(cleaned_val) > 4096:
            raise ToolForgeValidationError("secret value exceeds 4096 characters")
        now = _now()
        with self._lock:
            entries = self._read_raw()
            entries[validated_name] = {"value": cleaned_val, "updated_at": now}
            self._write_raw(entries)
        return {"name": validated_name, "configured": True, "updated_at": now}

    def delete_secret(self, name: str) -> bool:
        validated_name = validate_secret_name(name)
        with self._lock:
            entries = self._read_raw()
            if validated_name not in entries:
                return False
            entries.pop(validated_name, None)
            self._write_raw(entries)
            return True

    def get_map(self, allowed_names: Iterable[str] | None = None) -> dict[str, str]:
        allowed = set(allowed_names) if allowed_names is not None else None
        with self._lock:
            entries = self._read_raw()
            return {
                name: item["value"]
                for name, item in entries.items()
                if item["value"] and (allowed is None or name in allowed)
            }

    def all_secret_values(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(item["value"] for item in self._read_raw().values() if item["value"])


def ensure_forge_mcp_tokens(
    data_directory: Path,
    env_values: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Ensure deterministic per-tier bearer tokens shared across admin-serve and qq-serve."""
    env_pool = env_values if env_values is not None else os.environ
    env_overrides = {
        "admin": str(env_pool.get("COMPANY_AGENT_MCP_SECRET_FORGE_ADMIN") or "").strip(),
        "internal": str(env_pool.get("COMPANY_AGENT_MCP_SECRET_FORGE_INTERNAL") or "").strip(),
        "common": str(env_pool.get("COMPANY_AGENT_MCP_SECRET_FORGE_COMMON") or "").strip(),
    }
    token_path = Path(data_directory).resolve() / "forge-mcp-tokens.json"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(token_path.parent, 0o700)

    def _read_existing() -> dict[str, str]:
        if token_path.is_symlink():
            raise ToolForgeSecurityError("forge-mcp-tokens.json cannot be a symlink")
        if not token_path.is_file():
            return {}
        data = json.loads(token_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: str(data[k]) for k in ("admin", "internal", "common") if isinstance(data.get(k), str) and data[k]}
        return {}

    stored = _read_existing()
    if len(stored) < 3:
        generated = {
            tier: stored.get(tier) or secrets.token_urlsafe(32)
            for tier in ("admin", "internal", "common")
        }
        try:
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(generated, handle, indent=2)
            stored = generated
        except FileExistsError:
            stored = _read_existing()
            if len(stored) < 3:
                merged = {**generated, **stored}
                tmp = token_path.with_name(".forge-mcp-tokens.tmp")
                tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
                if os.name != "nt":
                    tmp.chmod(0o600)
                tmp.replace(token_path)
                stored = merged

    return {tier: env_overrides[tier] or stored[tier] for tier in ("admin", "internal", "common")}


def build_forge_mcp_specs(host: str = DEFAULT_FORGE_MCP_HOST, port: int = DEFAULT_FORGE_MCP_PORT) -> tuple[McpServerSpec, ...]:
    connect_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    base = f"http://{connect_host}:{port}"
    return (
        McpServerSpec(
            server_id="forge_admin",
            description="L3 管理员工具工坊 MCP（接口探测、编写草稿、试跑验证、提审上架与版本回滚）",
            transport="streamable-http",
            access_level="admin",
            enabled=True,
            url=f"{base}/mcp/forge",
            header_env_refs=(("Authorization", "Bearer ${COMPANY_AGENT_MCP_SECRET_FORGE_ADMIN}"),),
            tool_call_timeout_ms=90_000,
            fail_on_startup_error=False,
        ),
        McpServerSpec(
            server_id="forge_internal",
            description="已审批开放给员工群与管理员的自定义工具集 (internal)",
            transport="streamable-http",
            access_level="internal",
            enabled=True,
            url=f"{base}/mcp/internal",
            header_env_refs=(("Authorization", "Bearer ${COMPANY_AGENT_MCP_SECRET_FORGE_INTERNAL}"),),
            tool_call_timeout_ms=60_000,
            fail_on_startup_error=False,
        ),
        McpServerSpec(
            server_id="forge_common",
            description="已审批开放给全部对话的通用自定义工具集 (common)",
            transport="streamable-http",
            access_level="common",
            enabled=True,
            url=f"{base}/mcp/common",
            header_env_refs=(("Authorization", "Bearer ${COMPANY_AGENT_MCP_SECRET_FORGE_COMMON}"),),
            tool_call_timeout_ms=60_000,
            fail_on_startup_error=False,
        ),
    )


def forge_mcp_secrets_map(tokens: Mapping[str, str]) -> dict[str, str]:
    return {
        "COMPANY_AGENT_MCP_SECRET_FORGE_ADMIN": str(tokens["admin"]),
        "COMPANY_AGENT_MCP_SECRET_FORGE_INTERNAL": str(tokens["internal"]),
        "COMPANY_AGENT_MCP_SECRET_FORGE_COMMON": str(tokens["common"]),
    }


def _host_matches_domain_rule(hostname: str, rule_domain: str, allow_subdomains: bool) -> bool:
    host = hostname.lower().strip(".").strip("[]")
    target = rule_domain.lower().strip(".").strip("[]")
    if host == target:
        return True
    if allow_subdomains and host.endswith("." + target):
        return True
    return False


def _default_dns_resolver(hostname: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and isinstance(sockaddr[0], str):
            ips.append(sockaddr[0])
    return ips


def validate_outbound_url(
    raw_url: str,
    domain_rules: Sequence[Mapping[str, Any]],
    *,
    resolver: Callable[[str, int], list[str]] | None = None,
) -> str:
    url = str(raw_url or "").strip()
    if not url or any(ch in url for ch in "\r\n\x00"):
        raise ToolForgeSecurityError("URL must be a non-empty single-line string")
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ToolForgeSecurityError("URL must be HTTP(S) without embedded credentials or fragment")

    hostname = parsed.hostname.lower().strip("[]")
    if hostname in {"localhost", "ip6-localhost"} or hostname.endswith(".localhost"):
        raise ToolForgeSecurityError(f"forbidden loopback hostname: {hostname}")

    matched_rule = next(
        (rule for rule in domain_rules if _host_matches_domain_rule(hostname, str(rule["domain"]), bool(rule.get("allow_subdomains")))),
        None,
    )
    if matched_rule is None:
        raise ToolForgeSecurityError(f"outbound domain {hostname!r} is not in forge allowed_domains")

    explicit_private_ip = False
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if literal_ip.is_loopback or literal_ip.is_unspecified or literal_ip.is_link_local or literal_ip.is_multicast:
            raise ToolForgeSecurityError(f"forbidden loopback/reserved IP {literal_ip}")
        explicit_private_ip = literal_ip.is_private
        resolved_ips = [str(literal_ip)]
    else:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        resolve_fn = resolver or _default_dns_resolver
        try:
            resolved_ips = resolve_fn(hostname, port)
        except OSError as error:
            raise ToolForgeError(f"DNS resolution failed for {hostname}: {error}") from error
        if not resolved_ips:
            raise ToolForgeError(f"DNS resolution returned no addresses for {hostname}")

    for raw_ip in resolved_ips:
        addr = ipaddress.ip_address(raw_ip.split("%", 1)[0])
        if addr.is_loopback or addr.is_unspecified or addr.is_link_local or addr.is_multicast:
            raise ToolForgeSecurityError(f"resolved address {addr} is forbidden (loopback/link-local/multicast)")
        is_tun_fake_ip = (
            literal_ip is None
            and isinstance(addr, ipaddress.IPv4Address)
            and addr in _TUN_FAKE_IP_NETWORK
        )
        if addr.is_private and not explicit_private_ip and not is_tun_fake_ip:
            raise ToolForgeSecurityError(f"domain {hostname!r} resolved to private address {addr} without explicit IP allowlist")

    return url


def _expand_string_with_secrets(template: str, secret_map: Mapping[str, str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        if var_name not in secret_map:
            raise ToolForgeSecurityError(f"secret alias {var_name!r} is missing or not declared in secret_refs")
        return secret_map[var_name]

    return _TEMPLATE_REF_RE.sub(_replace, template)


def _expand_nested_with_secrets(value: Any, secret_map: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _expand_string_with_secrets(value, secret_map)
    if isinstance(value, list):
        return [_expand_nested_with_secrets(item, secret_map) for item in value]
    if isinstance(value, dict):
        return {str(k): _expand_nested_with_secrets(v, secret_map) for k, v in value.items()}
    return value


def extract_referenced_secrets(
    raw: Any,
    *,
    include_hmac_calls: bool = False,
    exclude_names: Iterable[str] = (),
) -> tuple[str, ...]:
    found: set[str] = set()
    excluded = {str(name).strip() for name in exclude_names if str(name).strip()}

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            found.update(name for name in _TEMPLATE_REF_RE.findall(node) if name not in excluded)
            if include_hmac_calls:
                found.update(name for name in _HMAC_CALL_SECRET_RE.findall(node) if name not in excluded)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, Mapping):
            for v in node.values():
                _walk(v)

    _walk(raw)
    return tuple(sorted(validate_secret_name(name) for name in found))


def execute_restricted_http(
    *,
    method: str,
    url: str,
    domain_rules: Sequence[Mapping[str, Any]],
    secret_map: Mapping[str, str],
    all_redacted_secrets: Iterable[str] = (),
    headers: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    raw_body: str | None = None,
    timeout_seconds: float = 15.0,
    resolver: Callable[[str, int], list[str]] | None = None,
    transport_opener: Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]] | None = None,
) -> dict[str, Any]:
    upper_method = str(method or "GET").strip().upper()
    if upper_method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        raise ToolForgeValidationError(f"unsupported HTTP method: {upper_method!r}")
    timeout = max(1.0, min(float(timeout_seconds), 45.0))

    expanded_url = _expand_string_with_secrets(url, secret_map)
    if params:
        expanded_params = _expand_nested_with_secrets(dict(params), secret_map)
        parsed = urllib.parse.urlsplit(expanded_url)
        existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        for k, v in expanded_params.items():
            if v is not None:
                existing.append((str(k), str(v)))
        expanded_url = urllib.parse.urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(existing),
            "",
        ))

    validate_outbound_url(expanded_url, domain_rules, resolver=resolver)

    req_headers: dict[str, str] = {"Accept": "application/json, text/plain;q=0.9, */*;q=0.8", "User-Agent": "CompanyAgentForge/1.0"}
    if headers:
        for k, v in headers.items():
            req_headers[str(k)] = _expand_string_with_secrets(str(v), secret_map)

    data_bytes: bytes | None = None
    if json_body is not None and upper_method not in {"GET", "HEAD"}:
        expanded_json = _expand_nested_with_secrets(json_body, secret_map)
        data_bytes = json.dumps(expanded_json, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    elif raw_body is not None and upper_method not in {"GET", "HEAD"}:
        data_bytes = _expand_string_with_secrets(str(raw_body), secret_map).encode("utf-8")

    request = urllib.request.Request(expanded_url, data=data_bytes, headers=req_headers, method=upper_method)

    try:
        if transport_opener is not None:
            status_code, resp_headers, raw_bytes = transport_opener(request, timeout)
        else:
            redirects_seen = [0]

            class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                    redirects_seen[0] += 1
                    if redirects_seen[0] > 3:
                        raise ToolForgeSecurityError("too many HTTP redirects")
                    validate_outbound_url(newurl, domain_rules, resolver=resolver)
                    return super().redirect_request(req, fp, code, msg, hdrs, newurl)

            opener = urllib.request.build_opener(_SafeRedirectHandler())
            try:
                with opener.open(request, timeout=timeout) as resp:
                    status_code = int(getattr(resp, "status", 200))
                    resp_headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                    raw_bytes = resp.read(MAX_HTTP_RESPONSE_BYTES + 1)
            except urllib.error.HTTPError as http_err:
                status_code = int(http_err.code)
                resp_headers = {str(k).lower(): str(v) for k, v in http_err.headers.items()} if http_err.headers else {}
                raw_bytes = http_err.read(MAX_HTTP_RESPONSE_BYTES + 1)
    except ToolForgeError:
        raise
    except Exception as error:
        raise ToolForgeError(f"HTTP request failed: {redact_text(str(error), all_redacted_secrets)}") from error

    truncated = len(raw_bytes) > MAX_HTTP_RESPONSE_BYTES
    body_text = raw_bytes[:MAX_HTTP_RESPONSE_BYTES].decode("utf-8", errors="replace")
    parsed_json: Any = None
    try:
        parsed_json = json.loads(body_text)
        sanitized_json = redact_value(parsed_json, all_redacted_secrets)
    except ValueError:
        sanitized_json = None

    safe_headers = {
        k: redact_text(v, all_redacted_secrets)
        for k, v in resp_headers.items()
        if k not in {"set-cookie", "authorization"}
    }
    return {
        "status_code": status_code,
        "headers": safe_headers,
        "json": sanitized_json,
        "text": None if sanitized_json is not None else redact_text(body_text[:16_000], all_redacted_secrets),
        "truncated": truncated,
    }


def validate_schema_definition(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        raise ToolForgeValidationError("input_schema must be a JSON Schema object")
    if schema.get("type") != "object":
        raise ToolForgeValidationError("input_schema.type must be 'object'")
    props = schema.get("properties")
    if props is None:
        props = {}
    if not isinstance(props, Mapping):
        raise ToolForgeValidationError("input_schema.properties must be an object")
    cleaned_props: dict[str, dict[str, Any]] = {}
    allowed_types = {"string", "integer", "number", "boolean", "array", "object"}
    for prop_name, raw_spec in props.items():
        if not isinstance(prop_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", prop_name):
            raise ToolForgeValidationError(f"invalid parameter name in input_schema: {prop_name!r}")
        if not isinstance(raw_spec, Mapping) or raw_spec.get("type") not in allowed_types:
            raise ToolForgeValidationError(f"parameter {prop_name!r} must declare a valid JSON Schema type")
        desc = str(raw_spec.get("description") or "").strip()
        if not desc:
            raise ToolForgeValidationError(f"parameter {prop_name!r} must include a clear description")
        cleaned_spec: dict[str, Any] = {"type": raw_spec["type"], "description": desc[:300]}
        for opt in ("default", "enum", "minimum", "maximum", "minLength", "maxLength", "items"):
            if opt in raw_spec:
                cleaned_spec[opt] = raw_spec[opt]
        cleaned_props[prop_name] = cleaned_spec
    required = schema.get("required") or []
    if not isinstance(required, list) or any(not isinstance(r, str) or r not in cleaned_props for r in required):
        raise ToolForgeValidationError("input_schema.required must list declared property names")
    return {
        "type": "object",
        "properties": cleaned_props,
        "required": list(dict.fromkeys(required)),
        "additionalProperties": False,
    }


def _check_json_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def validate_tool_arguments(schema: Mapping[str, Any], raw_args: Any) -> dict[str, Any]:
    args = {} if raw_args is None else raw_args
    if not isinstance(args, Mapping):
        raise ToolForgeValidationError("arguments must be a JSON object")
    props = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []

    unknown = [str(k) for k in args.keys() if k not in props]
    if unknown:
        raise ToolForgeValidationError(f"unknown argument(s): {', '.join(sorted(unknown))}")

    resolved: dict[str, Any] = {}
    for key, spec in props.items():
        if key not in args or args[key] is None:
            if "default" in spec:
                resolved[key] = spec["default"]
            elif key in required:
                raise ToolForgeValidationError(f"missing required argument: {key}")
            continue
        val = args[key]
        expected_type = spec["type"]
        if not _check_json_type(val, expected_type):
            raise ToolForgeValidationError(f"argument {key!r} must be of type {expected_type}")
        if "enum" in spec and val not in spec["enum"]:
            raise ToolForgeValidationError(f"argument {key!r} must be one of {spec['enum']}")
        if expected_type == "string":
            if "minLength" in spec and len(val) < int(spec["minLength"]):
                raise ToolForgeValidationError(f"argument {key!r} length below minLength")
            if "maxLength" in spec and len(val) > int(spec["maxLength"]):
                raise ToolForgeValidationError(f"argument {key!r} length exceeds maxLength")
        if expected_type in {"integer", "number"}:
            if "minimum" in spec and val < spec["minimum"]:
                raise ToolForgeValidationError(f"argument {key!r} below minimum")
            if "maximum" in spec and val > spec["maximum"]:
                raise ToolForgeValidationError(f"argument {key!r} exceeds maximum")
        resolved[key] = val

    for req in required:
        if req not in resolved:
            raise ToolForgeValidationError(f"missing required argument: {req}")
    return resolved


def validate_plugin_ast(source_code: str) -> ast.Module:
    if not isinstance(source_code, str) or not source_code.strip():
        raise ToolForgeValidationError("python_plugin code must not be empty")
    if len(source_code) > MAX_PLUGIN_CODE_CHARS:
        raise ToolForgeValidationError(f"python_plugin code exceeds {MAX_PLUGIN_CODE_CHARS} characters")
    try:
        tree = ast.parse(source_code, filename="<forge_plugin>", mode="exec")
    except SyntaxError as error:
        raise ToolForgeValidationError(f"Python syntax error at line {error.lineno}: {error.msg}") from error

    has_run = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            if len(node.args.posonlyargs) + len(node.args.args) < 2:
                raise ToolForgeValidationError("def run(args, ctx) must accept two arguments")
            has_run = True
    if not has_run:
        raise ToolForgeValidationError("python_plugin must define top-level function: def run(args, ctx)")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in _ALLOWED_PLUGIN_MODULES:
                    raise ToolForgeSecurityError(f"disallowed import in plugin: {alias.name!r}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level != 0 or mod not in _ALLOWED_PLUGIN_MODULES:
                raise ToolForgeSecurityError(f"disallowed from-import in plugin: {mod!r}")
            if any(alias.name == "*" for alias in node.names):
                raise ToolForgeSecurityError("wildcard import is forbidden in plugin")
        elif isinstance(node, ast.Attribute):
            if "__" in node.attr:
                raise ToolForgeSecurityError(f"dunder attribute access {node.attr!r} is forbidden in plugin")
        elif isinstance(node, ast.Name):
            if node.id in _BLOCKED_PLUGIN_NAMES or "__" in node.id:
                raise ToolForgeSecurityError(f"forbidden identifier in plugin: {node.id!r}")
    return tree


_PLUGIN_SUBPROCESS_BOOTSTRAP = r'''
import json
import sys
import urllib.request

payload = json.loads(sys.stdin.read())
callback_url = payload["callback_url"]
nonce = payload["nonce"]
source_code = payload["code"]
args = payload["args"]

def _rpc(action, body):
    req = urllib.request.Request(
        callback_url + "/" + action,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Forge-Nonce": nonce},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "host callback rejected request")
    return data["result"]

class _HttpProxy:
    def request(self, method, url, *, params=None, headers=None, json_body=None, raw_body=None, timeout=15.0):
        return _rpc("http", {
            "method": method,
            "url": url,
            "params": params,
            "headers": headers,
            "json_body": json_body,
            "raw_body": raw_body,
            "timeout": timeout,
        })
    def get(self, url, *, params=None, headers=None, timeout=15.0):
        return self.request("GET", url, params=params, headers=headers, timeout=timeout)
    def post(self, url, *, params=None, headers=None, json_body=None, raw_body=None, timeout=15.0):
        return self.request("POST", url, params=params, headers=headers, json_body=json_body, raw_body=raw_body, timeout=timeout)

class _Ctx:
    def __init__(self):
        self.http = _HttpProxy()
    def hmac_sha256(self, secret_name, message):
        return _rpc("hmac_sha256", {"secret_name": secret_name, "message": message})

safe_builtins = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "float": float, "int": int, "isinstance": isinstance,
    "len": len, "list": list, "max": max, "min": min, "pow": pow, "range": range,
    "reversed": reversed, "round": round, "set": set, "sorted": sorted,
    "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "ValueError": ValueError, "RuntimeError": RuntimeError, "KeyError": KeyError, "TypeError": TypeError,
    "__import__": __import__,
}
namespace = {"__builtins__": safe_builtins}
exec(compile(source_code, "<forge_plugin>", "exec"), namespace)
result = namespace["run"](args, _Ctx())
sys.stdout.write(json.dumps(result, ensure_ascii=False))
'''


def execute_python_plugin(
    *,
    code: str,
    args: dict[str, Any],
    domain_rules: Sequence[Mapping[str, Any]],
    secret_map: Mapping[str, str],
    all_redacted_secrets: Iterable[str] = (),
    resolver: Callable[[str, int], list[str]] | None = None,
    transport_opener: Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]] | None = None,
    timeout_seconds: float = PLUGIN_TIMEOUT_SECONDS,
) -> Any:
    validate_plugin_ast(code)
    nonce = secrets.token_urlsafe(24)
    http_calls = [0]

    class _CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            if not hmac.compare_digest(self.headers.get("X-Forge-Nonce", ""), nonce):
                return self._send_json(401, {"ok": False, "error": "unauthorized callback"})
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads((self.rfile.read(min(length, 512_000)) or b"{}").decode("utf-8"))
            try:
                if self.path == "/http":
                    http_calls[0] += 1
                    if http_calls[0] > 50:
                        raise ToolForgeSecurityError("plugin exceeded 50 outbound HTTP calls per invocation")
                    res = execute_restricted_http(
                        method=str(body.get("method") or "GET"),
                        url=str(body.get("url") or ""),
                        domain_rules=domain_rules,
                        secret_map=secret_map,
                        all_redacted_secrets=all_redacted_secrets,
                        headers=body.get("headers") if isinstance(body.get("headers"), Mapping) else None,
                        params=body.get("params") if isinstance(body.get("params"), Mapping) else None,
                        json_body=body.get("json_body"),
                        raw_body=body.get("raw_body"),
                        timeout_seconds=float(body.get("timeout") or 15.0),
                        resolver=resolver,
                        transport_opener=transport_opener,
                    )
                    return self._send_json(200, {"ok": True, "result": res})
                if self.path == "/hmac_sha256":
                    s_name = str(body.get("secret_name") or "").strip()
                    if s_name not in secret_map:
                        raise ToolForgeSecurityError(f"secret {s_name!r} is not declared for this tool")
                    digest = hmac.new(
                        secret_map[s_name].encode("utf-8"),
                        str(body.get("message") or "").encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()
                    return self._send_json(200, {"ok": True, "result": digest})
                return self._send_json(404, {"ok": False, "error": "unknown callback"})
            except Exception as exc:
                return self._send_json(200, {"ok": False, "error": redact_text(str(exc), all_redacted_secrets)})

    server = ThreadingHTTPServer(("127.0.0.1", 0), _CallbackHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        clean_env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if os.name == "nt" and "SYSTEMROOT" in os.environ:
            clean_env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        payload = json.dumps({
            "callback_url": f"http://127.0.0.1:{port}",
            "nonce": nonce,
            "code": code,
            "args": args,
        }, ensure_ascii=False)
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _PLUGIN_SUBPROCESS_BOOTSTRAP],
            input=payload,
            capture_output=True,
            text=True,
            env=clean_env,
            timeout=timeout_seconds,
            check=False,
        )
        if proc.returncode != 0:
            err = redact_text((proc.stderr or proc.stdout or "plugin execution failed").strip()[-1200:], all_redacted_secrets)
            raise ToolForgeError(f"python_plugin failed (exit {proc.returncode}): {err}")
        try:
            decoded = json.loads(proc.stdout)
        except ValueError as exc:
            raise ToolForgeError("python_plugin must return a JSON-serializable result") from exc
        return redact_value(decoded, all_redacted_secrets)
    except subprocess.TimeoutExpired as exc:
        raise ToolForgeError(f"python_plugin timed out after {timeout_seconds:.0f}s") from exc
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _extract_path(payload: Any, dotted_path: str) -> Any:
    current = payload
    for part in [p for p in dotted_path.split(".") if p]:
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _render_arg_template(value: Any, args: Mapping[str, Any], declared_keys: Iterable[str] = ()) -> Any:
    declared = set(declared_keys) | set(args.keys())
    if isinstance(value, str):
        full = _ARG_PLACEHOLDER_RE.fullmatch(value)
        if full:
            key = full.group(1) or full.group(2)
            if key in args:
                return args[key]
            if key in declared:
                return ""

        def _sub(match: re.Match[str]) -> str:
            key = match.group(1) or match.group(2)
            if key in args:
                return "" if args[key] is None else str(args[key])
            if key in declared:
                return ""
            return match.group(0)

        return _ARG_PLACEHOLDER_RE.sub(_sub, value)
    if isinstance(value, list):
        return [_render_arg_template(item, args, declared) for item in value]
    if isinstance(value, dict):
        return {str(k): _render_arg_template(v, args, declared) for k, v in value.items()}
    return value


class ToolForgeService:
    """Control-plane-backed lifecycle manager for admin-forged tools, secrets, domains, and versions."""

    def __init__(
        self,
        plane: ControlPlane,
        *,
        secrets_vault: ForgeSecretsVault | None = None,
        shared_tools: SharedToolsManager | None = None,
        dns_resolver: Callable[[str, int], list[str]] | None = None,
        http_opener: Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]] | None = None,
    ) -> None:
        self.plane = plane
        self.store = plane.store
        self.secrets_vault = secrets_vault or ForgeSecretsVault(Path.cwd() / "forge-secrets.json")
        self.shared_tools = shared_tools
        self.dns_resolver = dns_resolver
        self.http_opener = http_opener

    # ------------------------------------------------------------------
    # Domains & Secret Aliases
    # ------------------------------------------------------------------
    def list_domains(self) -> list[dict[str, Any]]:
        rows = self.store.fetchall(
            "SELECT domain, allow_subdomains, note, created_by, created_at FROM forge_allowed_domains ORDER BY domain ASC"
        )
        return [
            {
                "domain": row["domain"],
                "allow_subdomains": bool(row["allow_subdomains"]),
                "note": row["note"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def add_domain(self, domain: str, *, allow_subdomains: bool = False, note: str = "", created_by: str = "admin") -> dict[str, Any]:
        normalized = _normalize_domain(domain)
        now = _now()
        clean_note = str(note or "").strip()[:200]
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO forge_allowed_domains(domain, allow_subdomains, note, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(domain) DO UPDATE SET allow_subdomains=excluded.allow_subdomains, note=excluded.note",
                (normalized, 1 if allow_subdomains else 0, clean_note, created_by, now),
            )
            self.plane._audit(conn, "forge_domain", normalized, "saved", {"allow_subdomains": allow_subdomains}, now)
        return {"domain": normalized, "allow_subdomains": allow_subdomains, "note": clean_note, "created_at": now}

    def remove_domain(self, domain: str, *, actor: str = "admin") -> bool:
        normalized = _normalize_domain(domain)
        now = _now()
        with self.store.transaction() as conn:
            cur = conn.execute("DELETE FROM forge_allowed_domains WHERE domain=?", (normalized,))
            removed = cur.rowcount > 0
            if removed:
                self.plane._audit(conn, "forge_domain", normalized, "removed", {"actor": actor}, now)
            return removed

    def list_secrets_metadata(self) -> list[dict[str, Any]]:
        return self.secrets_vault.list_metadata()

    def set_secret(self, name: str, value: str, *, actor: str = "admin") -> dict[str, Any]:
        record = self.secrets_vault.set_secret(name, value)
        with self.store.transaction() as conn:
            self.plane._audit(conn, "forge_secret", record["name"], "saved", {"actor": actor}, _now())
        return record

    def delete_secret(self, name: str, *, actor: str = "admin") -> bool:
        removed = self.secrets_vault.delete_secret(name)
        if removed:
            with self.store.transaction() as conn:
                self.plane._audit(conn, "forge_secret", validate_secret_name(name), "deleted", {"actor": actor}, _now())
        return removed

    # ------------------------------------------------------------------
    # Probe & Draft
    # ------------------------------------------------------------------
    def probe_http(
        self,
        *,
        method: str = "GET",
        url: str,
        headers: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        secret_refs = extract_referenced_secrets({"url": url, "headers": headers or {}, "params": params or {}, "body": json_body})
        secret_map = self.secrets_vault.get_map(secret_refs)
        return execute_restricted_http(
            method=method,
            url=url,
            domain_rules=self.list_domains(),
            secret_map=secret_map,
            all_redacted_secrets=self.secrets_vault.all_secret_values(),
            headers=headers,
            params=params,
            json_body=json_body,
            timeout_seconds=timeout_seconds,
            resolver=self.dns_resolver,
            transport_opener=self.http_opener,
        )

    def draft_tool(
        self,
        *,
        tool_id: str,
        kind: str,
        description: str,
        when_to_use: str = "",
        when_not_to_use: str = "",
        operation_type: str = "read",
        target_visibility: str = "internal",
        input_schema: Mapping[str, Any] | None = None,
        http_spec: Mapping[str, Any] | None = None,
        python_code: str = "",
        script_name: str = "",
        script_content: str = "",
        allowed_domains: Sequence[str] | None = None,
        default_test_input: Mapping[str, Any] | None = None,
        created_by: str = "admin",
    ) -> dict[str, Any]:
        clean_id = str(tool_id or "").strip()
        if not TOOL_ID_RE.fullmatch(clean_id) or clean_id.startswith("forge_"):
            raise ToolForgeValidationError("tool_id must match ^[a-z][a-z0-9_]{2,47}$ and cannot start with 'forge_'")
        if kind not in FORGE_TOOL_KINDS:
            raise ToolForgeValidationError(f"kind must be one of {FORGE_TOOL_KINDS}")
        if operation_type not in FORGE_OPERATION_TYPES:
            raise ToolForgeValidationError(f"operation_type must be one of {FORGE_OPERATION_TYPES}")
        if operation_type == "write":
            operation_type = "mutating"
        if target_visibility not in MCP_ACCESS_LEVELS:
            raise ToolForgeValidationError(f"target_visibility must be one of {MCP_ACCESS_LEVELS}")
        clean_desc = str(description or "").strip()
        if len(clean_desc) < 10 or len(clean_desc) > 500:
            raise ToolForgeValidationError("description must be 10 to 500 characters")
        clean_when = str(when_to_use or "").strip()[:400]
        clean_not = str(when_not_to_use or "").strip()[:400]

        schema = validate_schema_definition(input_schema or {"type": "object", "properties": {}, "required": []})
        test_args = validate_tool_arguments(schema, default_test_input or {})

        normalized_domains = tuple(sorted({_normalize_domain(d) for d in (allowed_domains or ()) if str(d).strip()}))
        secret_refs: tuple[str, ...] = ()
        definition: dict[str, Any] = {
            "tool_id": clean_id,
            "kind": kind,
            "description": clean_desc,
            "when_to_use": clean_when,
            "when_not_to_use": clean_not,
            "operation_type": operation_type,
            "target_visibility": target_visibility,
            "input_schema": schema,
            "default_test_input": test_args,
        }

        if kind == "http_api":
            if not isinstance(http_spec, Mapping):
                raise ToolForgeValidationError("http_spec is required for kind='http_api'")
            method = str(http_spec.get("method") or "GET").strip().upper()
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                raise ToolForgeValidationError("http_spec.method must be GET, POST, PUT, PATCH, or DELETE")
            url_tpl = str(http_spec.get("url_template") or http_spec.get("url") or "").strip()
            if not url_tpl:
                raise ToolForgeValidationError("http_spec.url_template is required")
            parsed_host = (urllib.parse.urlsplit(url_tpl).hostname or "").lower()
            if not parsed_host:
                raise ToolForgeValidationError("http_spec.url_template must include a valid host")
            domain_set = set(normalized_domains)
            if not domain_set:
                domain_set.add(_normalize_domain(parsed_host))
            normalized_domains = tuple(sorted(domain_set))
            headers_tpl = dict(http_spec.get("headers") or {})
            query_tpl = dict(http_spec.get("query") or http_spec.get("query_template") or {})
            body_tpl = http_spec["body"] if "body" in http_spec else http_spec.get("json_body_template")
            response_path = str(http_spec.get("response_path") or http_spec.get("result_path") or "").strip()
            timeout_sec = max(1.0, min(float(http_spec.get("timeout_seconds") or 15.0), 45.0))
            spec_clean = {
                "method": method,
                "url_template": url_tpl,
                "headers": headers_tpl,
                "query": query_tpl,
                "body": body_tpl,
                "response_path": response_path,
                "timeout_seconds": timeout_sec,
            }
            declared_param_names = tuple((schema.get("properties") or {}).keys())
            secret_refs = extract_referenced_secrets(spec_clean, exclude_names=declared_param_names)
            definition["http_spec"] = spec_clean
        elif kind == "python_plugin":
            validate_plugin_ast(python_code)
            declared_param_names = tuple((schema.get("properties") or {}).keys())
            secret_refs = extract_referenced_secrets(
                python_code,
                include_hmac_calls=True,
                exclude_names=declared_param_names,
            )
            explicit_refs = http_spec.get("secret_refs") if isinstance(http_spec, Mapping) else None
            if isinstance(explicit_refs, list):
                secret_refs = tuple(sorted(set(secret_refs) | {validate_secret_name(x) for x in explicit_refs}))
            definition["python_code"] = python_code.replace("\r\n", "\n")
        else:
            s_name = str(script_name or clean_id).strip()
            if not SCRIPT_NAME_PATTERN.fullmatch(s_name):
                raise ToolForgeValidationError("script_name must be 1-64 alphanumeric, dot, underscore, or dash characters")
            if not isinstance(script_content, str) or not script_content.strip():
                raise ToolForgeValidationError("script_content is required for kind='shared_script'")
            if target_visibility == "admin":
                raise ToolForgeValidationError("shared_script target_visibility should be 'internal' or 'common'")
            definition["script_name"] = s_name
            definition["script_content"] = script_content.replace("\r\n", "\n")

        definition["allowed_domains"] = list(normalized_domains)
        definition["secret_refs"] = list(secret_refs)
        code_hash = hashlib.sha256(json.dumps(definition, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        now = _now()

        with self.store.transaction() as conn:
            existing = conn.execute("SELECT tool_id, latest_version FROM forge_tools WHERE tool_id=?", (clean_id,)).fetchone()
            if existing is None:
                version = 1
                conn.execute(
                    "INSERT INTO forge_tools(tool_id, kind, visibility, operation_type, active_version, latest_version, enabled, description, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 0, 1, 1, ?, ?, ?, ?)",
                    (clean_id, kind, target_visibility, operation_type, clean_desc, created_by, now, now),
                )
            else:
                version = int(existing["latest_version"]) + 1
                conn.execute(
                    "UPDATE forge_tools SET kind=?, operation_type=?, latest_version=?, description=?, updated_at=? WHERE tool_id=?",
                    (kind, operation_type, version, clean_desc, now, clean_id),
                )
            conn.execute(
                "INSERT INTO forge_tool_versions(tool_id, version, status, definition_json, test_report_json, code_hash, created_by, approved_by, created_at, approved_at) "
                "VALUES (?, ?, 'draft', ?, NULL, ?, ?, NULL, ?, NULL)",
                (clean_id, version, json.dumps(definition, ensure_ascii=False), code_hash, created_by, now),
            )
            self.plane._audit(conn, "forge_tool", f"{clean_id}@v{version}", "drafted", {"kind": kind, "code_hash": code_hash}, now)

        return {
            "tool_id": clean_id,
            "version": version,
            "status": "draft",
            "kind": kind,
            "code_hash": code_hash,
            "allowed_domains": list(normalized_domains),
            "secret_refs": list(secret_refs),
        }

    # ------------------------------------------------------------------
    # Execution & Verification
    # ------------------------------------------------------------------
    def _load_version_row(self, tool_id: str, version: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        tool_row = self.store.fetchone("SELECT * FROM forge_tools WHERE tool_id=?", (tool_id,))
        if tool_row is None:
            raise ToolForgeValidationError(f"unknown forge tool: {tool_id}")
        target_v = int(version if version is not None else (tool_row["latest_version"] or tool_row["active_version"]))
        ver_row = self.store.fetchone("SELECT * FROM forge_tool_versions WHERE tool_id=? AND version=?", (tool_id, target_v))
        if ver_row is None:
            raise ToolForgeValidationError(f"version {target_v} not found for tool {tool_id}")
        return dict(tool_row), {
            **dict(ver_row),
            "definition": json.loads(ver_row["definition_json"]),
            "test_report": json.loads(ver_row["test_report_json"]) if ver_row["test_report_json"] else None,
        }

    def _execute_definition(self, definition: Mapping[str, Any], arguments: Mapping[str, Any] | None) -> Any:
        schema = definition.get("input_schema") or {"type": "object", "properties": {}, "required": []}
        validated_args = validate_tool_arguments(schema, arguments)
        kind = str(definition["kind"])
        if kind == "shared_script":
            syntax_error = None
            script_name = str(definition.get("script_name") or "")
            content = str(definition.get("script_content") or "")
            if script_name.endswith(".py") or content.lstrip().startswith("#!/usr/bin/env python"):
                try:
                    ast.parse(content, filename=script_name)
                except SyntaxError as error:
                    syntax_error = f"Python syntax error line {error.lineno}: {error.msg}"
            if syntax_error:
                raise ToolForgeValidationError(syntax_error)
            return {
                "validated": True,
                "script_name": script_name,
                "bytes": len(content.encode("utf-8")),
                "lines": len(content.splitlines()),
                "preview": content[:800],
            }

        domain_rules = self.list_domains()
        declared_domains = definition.get("allowed_domains") or []
        if declared_domains:
            allowed_set = {rule["domain"]: rule for rule in domain_rules}
            filtered_rules = [rule for rule in domain_rules if rule["domain"] in declared_domains or any(
                _host_matches_domain_rule(d, rule["domain"], bool(rule.get("allow_subdomains"))) for d in declared_domains
            )]
            if not filtered_rules and any(declared not in allowed_set for declared in declared_domains):
                filtered_rules = domain_rules
            domain_rules = filtered_rules

        declared_param_names = tuple((schema.get("properties") or {}).keys())
        secret_refs = tuple(s for s in (definition.get("secret_refs") or ()) if s not in declared_param_names)
        secret_map = self.secrets_vault.get_map(secret_refs)
        all_secrets = self.secrets_vault.all_secret_values()

        if kind == "http_api":
            spec = dict(definition["http_spec"])
            url_rendered = spec["url_template"]
            used_in_url: set[str] = set()
            for match in _ARG_PLACEHOLDER_RE.finditer(url_rendered):
                key = match.group(1) or match.group(2)
                if key in validated_args or key in declared_param_names:
                    used_in_url.add(key)
            for k in used_in_url:
                raw_val = validated_args.get(k)
                qval = urllib.parse.quote("" if raw_val is None else str(raw_val), safe="")
                url_rendered = re.sub(
                    r"\{\{\s*(?:args\.)?" + re.escape(k) + r"\s*\}\}|\{(?:args\.)?" + re.escape(k) + r"\}",
                    qval,
                    url_rendered,
                )
            headers_rendered = _render_arg_template(spec.get("headers") or {}, validated_args, declared_param_names)
            query_rendered = _render_arg_template(spec.get("query") or {}, validated_args, declared_param_names)
            if spec["method"] in {"GET", "DELETE"} and not spec.get("query"):
                query_rendered = {k: v for k, v in validated_args.items() if k not in used_in_url and v is not None}
            body_rendered = (
                _render_arg_template(spec.get("body"), validated_args, declared_param_names)
                if spec.get("body") is not None
                else None
            )
            if body_rendered is None and spec["method"] in {"POST", "PUT", "PATCH"}:
                body_rendered = {k: v for k, v in validated_args.items() if k not in used_in_url}
            res = execute_restricted_http(
                method=spec["method"],
                url=url_rendered,
                domain_rules=domain_rules,
                secret_map=secret_map,
                all_redacted_secrets=all_secrets,
                headers=headers_rendered,
                params=query_rendered,
                json_body=body_rendered,
                timeout_seconds=float(spec.get("timeout_seconds") or 15.0),
                resolver=self.dns_resolver,
                transport_opener=self.http_opener,
            )
            if spec.get("response_path") and res.get("json") is not None:
                res["extracted"] = _extract_path(res["json"], str(spec["response_path"]))
            return res

        return execute_python_plugin(
            code=str(definition["python_code"]),
            args=validated_args,
            domain_rules=domain_rules,
            secret_map=secret_map,
            all_redacted_secrets=all_secrets,
            resolver=self.dns_resolver,
            transport_opener=self.http_opener,
        )

    def test_tool(
        self,
        tool_id: str,
        *,
        version: int | None = None,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _, ver = self._load_version_row(tool_id, version)
        defn = ver["definition"]
        test_args = arguments if arguments is not None else defn.get("default_test_input") or {}
        started = time.monotonic()
        try:
            output = self._execute_definition(defn, test_args)
            duration_ms = int((time.monotonic() - started) * 1000)
            http_status_err = isinstance(output, dict) and int(output.get("status_code") or 200) >= 400
            report = {
                "ok": not http_status_err,
                "checked_code_hash": ver["code_hash"],
                "tested_at": _now(),
                "duration_ms": duration_ms,
                "arguments": redact_value(test_args, self.secrets_vault.all_secret_values()),
                "sample_output": redact_value(output, self.secrets_vault.all_secret_values()),
                "error": f"HTTP {output.get('status_code')}" if http_status_err else "",
            }
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            report = {
                "ok": False,
                "checked_code_hash": ver["code_hash"],
                "tested_at": _now(),
                "duration_ms": duration_ms,
                "arguments": redact_value(test_args, self.secrets_vault.all_secret_values()),
                "sample_output": None,
                "error": redact_text(str(exc), self.secrets_vault.all_secret_values()),
            }

        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE forge_tool_versions SET test_report_json=? WHERE tool_id=? AND version=?",
                (json.dumps(report, ensure_ascii=False), tool_id, int(ver["version"])),
            )
            self.plane._audit(
                conn, "forge_tool", f"{tool_id}@v{ver['version']}", "tested",
                {"ok": report["ok"], "duration_ms": duration_ms}, _now(),
            )
        return {"tool_id": tool_id, "version": int(ver["version"]), **report}

    def call_active_tool(self, tool_id: str, arguments: Mapping[str, Any] | None, *, caller_tier: str) -> Any:
        tool, ver = self._load_active_version_for_tier(tool_id, caller_tier)
        started = time.monotonic()
        error_code = ""
        try:
            result = self._execute_definition(ver["definition"], arguments)
            return result
        except ToolForgeError as error:
            error_code = type(error).__name__
            raise
        except Exception as error:
            error_code = type(error).__name__
            raise ToolForgeError(redact_text(str(error), self.secrets_vault.all_secret_values())) from error
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._record_call_log(
                tool_id=tool["tool_id"],
                version=int(ver["version"]),
                caller_tier=caller_tier,
                duration_ms=duration_ms,
                ok=not bool(error_code),
                error_code=error_code,
            )

    def _load_active_version_for_tier(self, tool_id: str, caller_tier: str) -> tuple[dict[str, Any], dict[str, Any]]:
        tool = self.store.fetchone("SELECT * FROM forge_tools WHERE tool_id=?", (tool_id,))
        if tool is None or not int(tool["enabled"]) or int(tool["active_version"]) <= 0:
            raise ToolForgeValidationError(f"tool {tool_id!r} is not active")
        vis = str(tool["visibility"])
        allowed = (
            caller_tier == "admin"
            or (caller_tier == "internal" and vis in {"internal", "common"})
            or (caller_tier == "common" and vis == "common")
        )
        if not allowed:
            raise ToolForgeSecurityError(f"tool {tool_id!r} (visibility={vis}) is not authorized for tier {caller_tier!r}")
        _, ver = self._load_version_row(tool_id, int(tool["active_version"]))
        if ver["definition"].get("kind") == "shared_script":
            raise ToolForgeValidationError("shared_script runs inside bash shared-tools, not as an MCP call")
        return dict(tool), ver

    def _record_call_log(self, *, tool_id: str, version: int, caller_tier: str, duration_ms: int, ok: bool, error_code: str) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO forge_call_logs(call_id, tool_id, version, caller_tier, duration_ms, ok, error_code, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"fcall-{uuid4().hex[:16]}", tool_id, version, caller_tier, duration_ms, 1 if ok else 0, error_code[:80], _now()),
            )

    # ------------------------------------------------------------------
    # Proposals, Approval, Enable/Disable, Rollback
    # ------------------------------------------------------------------
    def submit_release(
        self,
        tool_id: str,
        *,
        version: int | None = None,
        target_visibility: str | None = None,
        requested_by: str = "admin",
        source_conversation_id: str = "",
        summary: str = "",
    ) -> dict[str, Any]:
        tool, ver = self._load_version_row(tool_id, version)
        defn = ver["definition"]
        report = ver["test_report"]
        if not isinstance(report, dict) or not report.get("ok"):
            raise ToolForgeValidationError("tool must pass forge_test_tool before submitting for approval")
        if report.get("checked_code_hash") != ver["code_hash"]:
            raise ToolForgeValidationError("draft modified since last passing test; re-run forge_test_tool first")

        vis = str(target_visibility or defn.get("target_visibility") or tool["visibility"])
        if vis not in MCP_ACCESS_LEVELS:
            raise ToolForgeValidationError(f"target_visibility must be one of {MCP_ACCESS_LEVELS}")

        domains = defn.get("allowed_domains") or []
        if domains:
            rules = self.list_domains()
            missing = [
                d for d in domains
                if not any(_host_matches_domain_rule(d, rule["domain"], bool(rule.get("allow_subdomains"))) for rule in rules)
            ]
            if missing:
                raise ToolForgeSecurityError(f"allowlisted domain(s) missing before release: {', '.join(missing)}")

        declared_props = set(((defn.get("input_schema") or {}).get("properties") or {}).keys())
        effective_secret_refs = [s for s in (defn.get("secret_refs") or ()) if s not in declared_props]
        missing_secrets = [
            s for s in effective_secret_refs
            if s not in self.secrets_vault.get_map([s])
        ]
        if missing_secrets:
            raise ToolForgeSecurityError(f"required secret alias(es) not configured in vault: {', '.join(missing_secrets)}")

        proposal_id = f"fprop-{uuid4().hex[:10]}"
        now = _now()
        clean_summary = str(summary or "").strip()[:500] or f"发布 {tool_id}@v{ver['version']} ({defn['kind']}, {tool['operation_type']}) 到 {vis}"
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE forge_tool_versions SET status='pending_approval' WHERE tool_id=? AND version=?",
                (tool_id, int(ver["version"])),
            )
            conn.execute(
                "INSERT INTO forge_proposals(proposal_id, tool_id, target_version, action, target_visibility, requested_by, source_conversation_id, summary, status, decided_by, reason, created_at, decided_at) "
                "VALUES (?, ?, ?, 'activate', ?, ?, ?, ?, 'pending', NULL, '', ?, NULL)",
                (proposal_id, tool_id, int(ver["version"]), vis, requested_by, source_conversation_id, clean_summary, now),
            )
            self.plane._audit(
                conn, "forge_proposal", proposal_id, "submitted",
                {"tool_id": tool_id, "version": int(ver["version"]), "target_visibility": vis}, now,
            )

        return {
            "proposal_id": proposal_id,
            "tool_id": tool_id,
            "target_version": int(ver["version"]),
            "target_visibility": vis,
            "kind": defn["kind"],
            "operation_type": tool["operation_type"],
            "allowed_domains": domains,
            "secret_refs": defn.get("secret_refs") or [],
            "summary": clean_summary,
            "status": "pending",
        }

    def approve_proposal(self, proposal_id: str, *, decided_by: str, is_admin: bool) -> dict[str, Any]:
        if not is_admin:
            raise ToolForgeSecurityError("only an authenticated L3 admin can approve tool releases")
        row = self.store.fetchone("SELECT * FROM forge_proposals WHERE proposal_id=?", (proposal_id,))
        if row is None:
            raise ToolForgeValidationError(f"proposal not found: {proposal_id}")
        if row["status"] != "pending":
            raise ToolForgeValidationError(f"proposal {proposal_id} is already {row['status']}")

        tool_id = str(row["tool_id"])
        target_v = int(row["target_version"])
        target_vis = str(row["target_visibility"])
        tool, ver = self._load_version_row(tool_id, target_v)
        defn = ver["definition"]
        now = _now()

        if defn.get("kind") == "shared_script":
            if self.shared_tools is None:
                raise ToolForgeError("SharedToolsManager not configured for shared_script approval")
            layer = "common" if target_vis == "common" else "internal"
            self.shared_tools.save_script(
                layer,
                str(defn["script_name"]),
                str(defn["script_content"]),
                description=str(defn["description"]),
            )

        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE forge_tool_versions SET status='superseded' WHERE tool_id=? AND status='active'",
                (tool_id,),
            )
            conn.execute(
                "UPDATE forge_tool_versions SET status='active', approved_by=?, approved_at=? WHERE tool_id=? AND version=?",
                (decided_by, now, tool_id, target_v),
            )
            conn.execute(
                "UPDATE forge_tools SET visibility=?, active_version=?, enabled=1, updated_at=? WHERE tool_id=?",
                (target_vis, target_v, now, tool_id),
            )
            conn.execute(
                "UPDATE forge_proposals SET status='approved', decided_by=?, decided_at=? WHERE proposal_id=?",
                (decided_by, now, proposal_id),
            )
            self.plane._audit(
                conn, "forge_proposal", proposal_id, "approved",
                {"tool_id": tool_id, "version": target_v, "visibility": target_vis, "decided_by": decided_by}, now,
            )

        return {
            "proposal_id": proposal_id,
            "status": "approved",
            "tool_id": tool_id,
            "active_version": target_v,
            "visibility": target_vis,
            "kind": defn["kind"],
            "mcp_server_id": None if defn["kind"] == "shared_script" else ("forge_common" if target_vis == "common" else ("forge_internal" if target_vis == "internal" else "forge_admin")),
        }

    def reject_proposal(self, proposal_id: str, *, decided_by: str, is_admin: bool, reason: str = "") -> dict[str, Any]:
        if not is_admin:
            raise ToolForgeSecurityError("only an authenticated L3 admin can reject tool releases")
        row = self.store.fetchone("SELECT * FROM forge_proposals WHERE proposal_id=?", (proposal_id,))
        if row is None:
            raise ToolForgeValidationError(f"proposal not found: {proposal_id}")
        if row["status"] != "pending":
            raise ToolForgeValidationError(f"proposal {proposal_id} is already {row['status']}")
        clean_reason = str(reason or "").strip()[:300]
        now = _now()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE forge_tool_versions SET status='rejected' WHERE tool_id=? AND version=?",
                (row["tool_id"], int(row["target_version"])),
            )
            conn.execute(
                "UPDATE forge_proposals SET status='rejected', decided_by=?, reason=?, decided_at=? WHERE proposal_id=?",
                (decided_by, clean_reason, now, proposal_id),
            )
            self.plane._audit(conn, "forge_proposal", proposal_id, "rejected", {"reason": clean_reason, "decided_by": decided_by}, now)
        return {"proposal_id": proposal_id, "status": "rejected", "tool_id": row["tool_id"], "reason": clean_reason}

    def set_tool_enabled(self, tool_id: str, *, enabled: bool, actor: str = "admin") -> dict[str, Any]:
        tool = self.store.fetchone("SELECT * FROM forge_tools WHERE tool_id=?", (tool_id,))
        if tool is None:
            raise ToolForgeValidationError(f"unknown forge tool: {tool_id}")
        now = _now()
        with self.store.transaction() as conn:
            conn.execute("UPDATE forge_tools SET enabled=?, updated_at=? WHERE tool_id=?", (1 if enabled else 0, now, tool_id))
            self.plane._audit(conn, "forge_tool", tool_id, "enabled" if enabled else "disabled", {"actor": actor}, now)
        return {"tool_id": tool_id, "enabled": bool(enabled), "active_version": int(tool["active_version"])}

    def rollback_tool(self, tool_id: str, *, target_version: int | None = None, actor: str = "admin") -> dict[str, Any]:
        tool = self.store.fetchone("SELECT * FROM forge_tools WHERE tool_id=?", (tool_id,))
        if tool is None:
            raise ToolForgeValidationError(f"unknown forge tool: {tool_id}")
        current_active = int(tool["active_version"])
        versions = self.store.fetchall(
            "SELECT * FROM forge_tool_versions WHERE tool_id=? AND version<? AND approved_at IS NOT NULL ORDER BY version DESC",
            (tool_id, current_active if current_active > 0 else 10**9),
        )
        chosen = None
        if target_version is not None:
            chosen = self.store.fetchone(
                "SELECT * FROM forge_tool_versions WHERE tool_id=? AND version=? AND approved_at IS NOT NULL",
                (tool_id, int(target_version)),
            )
            if chosen is None:
                raise ToolForgeValidationError(f"version v{target_version} was never approved for {tool_id}")
        elif versions:
            chosen = versions[0]
        else:
            raise ToolForgeValidationError(f"no prior approved version available to roll back {tool_id}")

        new_active = int(chosen["version"])
        defn = json.loads(chosen["definition_json"])
        vis = str(defn.get("target_visibility") or tool["visibility"])
        now = _now()
        if defn.get("kind") == "shared_script" and self.shared_tools is not None:
            layer = "common" if vis == "common" else "internal"
            self.shared_tools.save_script(layer, str(defn["script_name"]), str(defn["script_content"]), description=str(defn["description"]))

        with self.store.transaction() as conn:
            conn.execute("UPDATE forge_tool_versions SET status='rolled_back' WHERE tool_id=? AND status='active'", (tool_id,))
            conn.execute("UPDATE forge_tool_versions SET status='active' WHERE tool_id=? AND version=?", (tool_id, new_active))
            conn.execute("UPDATE forge_tools SET active_version=?, visibility=?, enabled=1, updated_at=? WHERE tool_id=?", (new_active, vis, now, tool_id))
            self.plane._audit(conn, "forge_tool", tool_id, "rolled_back", {"from": current_active, "to": new_active, "actor": actor}, now)
        return {"tool_id": tool_id, "previous_version": current_active, "active_version": new_active, "visibility": vis}

    # ------------------------------------------------------------------
    # Read / Query / MCP Schema Export
    # ------------------------------------------------------------------
    def list_active_tools_for_tier(self, tier: str) -> list[dict[str, Any]]:
        rows = self.store.fetchall("SELECT * FROM forge_tools WHERE enabled=1 AND active_version>0 ORDER BY tool_id ASC")
        items: list[dict[str, Any]] = []
        for row in rows:
            vis = str(row["visibility"])
            allowed = (
                tier == "admin"
                or (tier == "internal" and vis in {"internal", "common"})
                or (tier == "common" and vis == "common")
            )
            if not allowed:
                continue
            ver = self.store.fetchone(
                "SELECT definition_json FROM forge_tool_versions WHERE tool_id=? AND version=?",
                (row["tool_id"], int(row["active_version"])),
            )
            if ver is None:
                continue
            defn = json.loads(ver["definition_json"])
            if defn.get("kind") == "shared_script":
                continue
            items.append({
                "tool_id": row["tool_id"],
                "version": int(row["active_version"]),
                "kind": row["kind"],
                "visibility": vis,
                "operation_type": row["operation_type"],
                "description": defn.get("description") or row["description"],
                "when_to_use": defn.get("when_to_use") or "",
                "when_not_to_use": defn.get("when_not_to_use") or "",
                "input_schema": defn.get("input_schema") or {"type": "object", "properties": {}, "required": []},
            })
        return items

    def list_tools(self) -> list[dict[str, Any]]:
        rows = self.store.fetchall("SELECT * FROM forge_tools ORDER BY updated_at DESC")
        stats_rows = self.store.fetchall(
            "SELECT tool_id, COUNT(*) AS calls, SUM(ok) AS ok_calls, CAST(AVG(duration_ms) AS INTEGER) AS avg_ms FROM forge_call_logs GROUP BY tool_id"
        )
        stats_map = {
            r["tool_id"]: {"calls": int(r["calls"]), "ok_calls": int(r["ok_calls"] or 0), "avg_ms": int(r["avg_ms"] or 0)}
            for r in stats_rows
        }
        result: list[dict[str, Any]] = []
        for row in rows:
            ver_rows = self.store.fetchall(
                "SELECT version, status, code_hash, created_by, approved_by, created_at, approved_at, test_report_json "
                "FROM forge_tool_versions WHERE tool_id=? ORDER BY version DESC LIMIT 10",
                (row["tool_id"],),
            )
            versions = []
            for v in ver_rows:
                rep = json.loads(v["test_report_json"]) if v["test_report_json"] else None
                versions.append({
                    "version": int(v["version"]),
                    "status": v["status"],
                    "code_hash": v["code_hash"],
                    "created_by": v["created_by"],
                    "approved_by": v["approved_by"],
                    "created_at": v["created_at"],
                    "approved_at": v["approved_at"],
                    "test_ok": bool(rep and rep.get("ok")),
                    "test_error": (rep or {}).get("error") or "",
                })
            result.append({
                "tool_id": row["tool_id"],
                "kind": row["kind"],
                "visibility": row["visibility"],
                "operation_type": row["operation_type"],
                "active_version": int(row["active_version"]),
                "latest_version": int(row["latest_version"]),
                "enabled": bool(row["enabled"]),
                "description": row["description"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "stats": stats_map.get(row["tool_id"], {"calls": 0, "ok_calls": 0, "avg_ms": 0}),
                "versions": versions,
            })
        return result

    def get_tool(self, tool_id: str, *, version: int | None = None) -> dict[str, Any]:
        tool, ver = self._load_version_row(tool_id, version)
        return {
            "tool_id": tool["tool_id"],
            "kind": tool["kind"],
            "visibility": tool["visibility"],
            "operation_type": tool["operation_type"],
            "active_version": int(tool["active_version"]),
            "latest_version": int(tool["latest_version"]),
            "enabled": bool(tool["enabled"]),
            "version": int(ver["version"]),
            "version_status": ver["status"],
            "code_hash": ver["code_hash"],
            "definition": ver["definition"],
            "test_report": ver["test_report"],
        }

    def list_proposals(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if status and status != "all":
            where = "WHERE p.status=? "
            params.append(status)
        params.append(max(1, min(int(limit), 200)))
        rows = self.store.fetchall(
            f"SELECT p.*, v.definition_json, v.test_report_json FROM forge_proposals p "
            f"LEFT JOIN forge_tool_versions v ON p.tool_id=v.tool_id AND p.target_version=v.version "
            f"{where}ORDER BY p.created_at DESC LIMIT ?",
            tuple(params),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            defn = json.loads(row["definition_json"]) if row["definition_json"] else {}
            rep = json.loads(row["test_report_json"]) if row["test_report_json"] else None
            out.append({
                "proposal_id": row["proposal_id"],
                "tool_id": row["tool_id"],
                "target_version": int(row["target_version"]),
                "action": row["action"],
                "target_visibility": row["target_visibility"],
                "requested_by": row["requested_by"],
                "source_conversation_id": row["source_conversation_id"],
                "summary": row["summary"],
                "status": row["status"],
                "decided_by": row["decided_by"],
                "reason": row["reason"],
                "created_at": row["created_at"],
                "decided_at": row["decided_at"],
                "kind": defn.get("kind", ""),
                "operation_type": defn.get("operation_type", "read"),
                "allowed_domains": defn.get("allowed_domains", []),
                "secret_refs": defn.get("secret_refs", []),
                "code_preview": (defn.get("python_code") or defn.get("script_content") or json.dumps(defn.get("http_spec") or {}, ensure_ascii=False, indent=2))[:4000],
                "test_report": rep,
            })
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "domains": self.list_domains(),
            "secrets": self.list_secrets_metadata(),
            "tools": self.list_tools(),
            "proposals": self.list_proposals(status="all", limit=100),
            "pending_proposals": len(self.list_proposals(status="pending", limit=100)),
        }
