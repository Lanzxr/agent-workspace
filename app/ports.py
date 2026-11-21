from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

DEFAULT_PORT_BASE = 28765
DEFAULT_ADMIN_PORT = DEFAULT_PORT_BASE
DEFAULT_KNOWLEDGE_API_PORT = DEFAULT_PORT_BASE + 1
DEFAULT_WEB_GATE_PORT = DEFAULT_PORT_BASE + 2
DEFAULT_FORGE_MCP_PORT = DEFAULT_PORT_BASE + 3
DEFAULT_DSSUPERAGENT_PORT = DEFAULT_PORT_BASE + 4
DEFAULT_SEARCH_PORT = DEFAULT_PORT_BASE + 5
DEFAULT_ANYTHINGLLM_PORT = DEFAULT_PORT_BASE + 6

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _parse_port_value(raw: str | None, default: int, name: str) -> int:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError as error:
        raise ValueError(f"{name} must be a valid TCP port number") from error
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return value


def _connect_host(bind_host: str) -> str:
    cleaned = (bind_host or "").strip()
    return "127.0.0.1" if cleaned in {"", "0.0.0.0", "::", "localhost"} else cleaned


def _extract_loopback_url_port(raw_url: str | None) -> int | None:
    text = (raw_url or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS:
        return None
    return parsed.port


@dataclass(frozen=True)
class PortSettings:
    """Single source of truth for local TCP ports and auto-derived internal URLs."""

    port_base: int
    admin_host: str
    admin_port: int
    knowledge_api_host: str
    knowledge_api_port: int
    web_gate_host: str
    web_gate_port: int
    forge_mcp_host: str
    forge_mcp_port: int
    dssuperagent_host: str
    dssuperagent_port: int
    search_port: int
    anythingllm_port: int
    dssuperagent_url: str
    knowledge_api_url: str
    web_gate_url: str
    web_gate_dsh_search_url: str
    web_search_url: str
    anythingllm_url: str


def resolve_port_settings(values: Mapping[str, str] | None = None) -> PortSettings:
    """Resolve all Company Agent, DSSuperAgent, and container bridge ports without collisions."""
    env = values or {}
    raw_base = (env.get("COMPANY_AGENT_PORT_BASE") or "").strip()
    if raw_base:
        port_base = _parse_port_value(raw_base, DEFAULT_PORT_BASE, "COMPANY_AGENT_PORT_BASE")
        if not 1024 <= port_base <= 65528:
            raise ValueError("COMPANY_AGENT_PORT_BASE must be between 1024 and 65528 (reserves 7 contiguous ports)")
    else:
        port_base = DEFAULT_PORT_BASE

    admin_host = (env.get("COMPANY_AGENT_ADMIN_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    knowledge_host = (env.get("COMPANY_AGENT_KNOWLEDGE_API_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    web_gate_host = (env.get("COMPANY_AGENT_WEB_GATE_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    forge_host = (env.get("COMPANY_AGENT_FORGE_MCP_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    dss_host = (env.get("COMPANY_AGENT_DSSUPERAGENT_HOST") or "127.0.0.1").strip() or "127.0.0.1"

    admin_port = _parse_port_value(env.get("COMPANY_AGENT_ADMIN_PORT"), port_base, "COMPANY_AGENT_ADMIN_PORT")
    knowledge_port = _parse_port_value(env.get("COMPANY_AGENT_KNOWLEDGE_API_PORT"), port_base + 1, "COMPANY_AGENT_KNOWLEDGE_API_PORT")
    web_gate_port = _parse_port_value(env.get("COMPANY_AGENT_WEB_GATE_PORT"), port_base + 2, "COMPANY_AGENT_WEB_GATE_PORT")
    forge_port = _parse_port_value(env.get("COMPANY_AGENT_FORGE_MCP_PORT"), port_base + 3, "COMPANY_AGENT_FORGE_MCP_PORT")

    explicit_dss_url = (env.get("COMPANY_AGENT_DSSUPERAGENT_URL") or "").strip().rstrip("/")
    dss_url_port = _extract_loopback_url_port(explicit_dss_url)
    dss_port = _parse_port_value(
        env.get("COMPANY_AGENT_DSSUPERAGENT_PORT"),
        dss_url_port if dss_url_port is not None else (port_base + 4),
        "COMPANY_AGENT_DSSUPERAGENT_PORT",
    )

    explicit_search_url = (env.get("COMPANY_AGENT_WEB_SEARCH_URL") or "").strip()
    search_url_port = _extract_loopback_url_port(explicit_search_url)
    search_port = _parse_port_value(
        env.get("COMPANY_AGENT_SEARCH_PORT"),
        search_url_port if search_url_port is not None else (port_base + 5),
        "COMPANY_AGENT_SEARCH_PORT",
    )

    explicit_allm_url = (env.get("ANYTHINGLLM_BASE_URL") or "").strip().rstrip("/")
    allm_url_port = _extract_loopback_url_port(explicit_allm_url)
    anythingllm_port = _parse_port_value(
        env.get("COMPANY_AGENT_ANYTHINGLLM_PORT"),
        allm_url_port if allm_url_port is not None else (port_base + 6),
        "COMPANY_AGENT_ANYTHINGLLM_PORT",
    )

    explicit_gate_url = (env.get("COMPANY_AGENT_WEB_GATE_URL") or "").strip()
    web_gate_url = explicit_gate_url or f"http://{_connect_host(web_gate_host)}:{web_gate_port}/internal/web/authorize"
    parsed_gate = urlsplit(web_gate_url)
    if parsed_gate.scheme not in {"http", "https"} or not parsed_gate.netloc or parsed_gate.username or parsed_gate.password:
        raise ValueError("COMPANY_AGENT_WEB_GATE_URL must be an HTTP(S) URL without embedded credentials")
    web_gate_dsh_search_url = urlunsplit((parsed_gate.scheme, parsed_gate.netloc, "/internal/web/dsh-search", "", ""))

    dssuperagent_url = explicit_dss_url or f"http://{_connect_host(dss_host)}:{dss_port}"
    knowledge_api_url = f"http://{_connect_host(knowledge_host)}:{knowledge_port}/internal/knowledge/search"
    web_search_url = explicit_search_url or f"http://127.0.0.1:{search_port}/search"
    anythingllm_url = explicit_allm_url or f"http://127.0.0.1:{anythingllm_port}"

    used: dict[int, str] = {}
    local_bindings: list[tuple[str, int]] = [
        ("COMPANY_AGENT_ADMIN_PORT (Admin Console)", admin_port),
        ("COMPANY_AGENT_KNOWLEDGE_API_PORT (Knowledge API)", knowledge_port),
        ("COMPANY_AGENT_WEB_GATE_PORT (Web Gate)", web_gate_port),
        ("COMPANY_AGENT_FORGE_MCP_PORT (Tool Forge MCP)", forge_port),
        ("COMPANY_AGENT_DSSUPERAGENT_PORT (DSSuperAgent)", dss_port),
    ]
    if (urlsplit(web_search_url).hostname or "").lower() in _LOOPBACK_HOSTS:
        local_bindings.append(("COMPANY_AGENT_SEARCH_PORT (SearXNG)", search_port))
    if (urlsplit(anythingllm_url).hostname or "").lower() in _LOOPBACK_HOSTS:
        local_bindings.append(("COMPANY_AGENT_ANYTHINGLLM_PORT (AnythingLLM)", anythingllm_port))

    for label, port_num in local_bindings:
        if port_num in used:
            raise ValueError(f"port conflict in configuration: {used[port_num]} and {label} both resolve to port {port_num}")
        used[port_num] = label

    return PortSettings(
        port_base=port_base,
        admin_host=admin_host,
        admin_port=admin_port,
        knowledge_api_host=knowledge_host,
        knowledge_api_port=knowledge_port,
        web_gate_host=web_gate_host,
        web_gate_port=web_gate_port,
        forge_mcp_host=forge_host,
        forge_mcp_port=forge_port,
        dssuperagent_host=dss_host,
        dssuperagent_port=dss_port,
        search_port=search_port,
        anythingllm_port=anythingllm_port,
        dssuperagent_url=dssuperagent_url,
        knowledge_api_url=knowledge_api_url,
        web_gate_url=web_gate_url,
        web_gate_dsh_search_url=web_gate_dsh_search_url,
        web_search_url=web_search_url,
        anythingllm_url=anythingllm_url,
    )


def assert_port_available(host: str, port: int, *, service_name: str, env_var: str) -> None:
    """Verify loopback TCP port is unbound before starting a daemon, giving an actionable error if occupied."""
    if port == 0:
        return
    bind_host = _connect_host(host)
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_host, int(port)))
        except OSError as error:
            raise RuntimeError(
                f"端口冲突：{service_name} 无法监听 {bind_host}:{port}（端口已被其他进程占用：{error}）。"
                f"请在 runtime.env 中调整 {env_var}，或设置 COMPANY_AGENT_PORT_BASE=<空闲起始端口> 一键平移所有默认端口。"
            ) from error


__all__ = [
    "DEFAULT_ADMIN_PORT",
    "DEFAULT_ANYTHINGLLM_PORT",
    "DEFAULT_DSSUPERAGENT_PORT",
    "DEFAULT_FORGE_MCP_PORT",
    "DEFAULT_KNOWLEDGE_API_PORT",
    "DEFAULT_PORT_BASE",
    "DEFAULT_SEARCH_PORT",
    "DEFAULT_WEB_GATE_PORT",
    "PortSettings",
    "assert_port_available",
    "resolve_port_settings",
]
