from __future__ import annotations

import hashlib
import hashlib
import hmac
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .ports import DEFAULT_SEARCH_PORT, DEFAULT_WEB_GATE_PORT, resolve_port_settings
from .web_search import SearxngSearch

from .runtime_config import RuntimeConfig


@dataclass(frozen=True)
class WebGateSettings:
    """Loopback capability gate used by DSSuperAgent's controlled web tools."""

    host: str = "127.0.0.1"
    port: int = DEFAULT_WEB_GATE_PORT
    api_key: str = ""
    search_url: str = f"http://127.0.0.1:{DEFAULT_SEARCH_PORT}/search"
    search_timeout_seconds: float = 20.0
    owner: str = "admin"

    @classmethod
    def load(cls, path: Path) -> "WebGateSettings":
        values = RuntimeConfig._parse_env(path) if path.is_file() else {}
        ports = resolve_port_settings(values)
        host = ports.web_gate_host
        port = ports.web_gate_port
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("COMPANY_AGENT_WEB_GATE_HOST must bind to loopback")
        api_key = (values.get("COMPANY_AGENT_WEB_GATE_API_KEY")
                   or values.get("COMPANY_AGENT_KNOWLEDGE_API_KEY") or "").strip()
        if api_key and len(api_key) < 32:
            raise ValueError("COMPANY_AGENT_WEB_GATE_API_KEY must contain at least 32 characters")
        search_url = ports.web_search_url
        parsed_search_url = urlsplit(search_url)
        if (parsed_search_url.scheme not in {"http", "https"} or not parsed_search_url.hostname
                or parsed_search_url.username or parsed_search_url.password or parsed_search_url.query
                or parsed_search_url.fragment):
            raise ValueError("COMPANY_AGENT_WEB_SEARCH_URL must be an HTTP(S) endpoint without credentials or query")
        try:
            search_timeout = float(values.get("COMPANY_AGENT_WEB_SEARCH_TIMEOUT_SECONDS", "20"))
        except ValueError as error:
            raise ValueError("COMPANY_AGENT_WEB_SEARCH_TIMEOUT_SECONDS must be numeric") from error
        if not 1 <= search_timeout <= 60:
            raise ValueError("COMPANY_AGENT_WEB_SEARCH_TIMEOUT_SECONDS must be between 1 and 60")
        owner = (values.get("COMPANY_AGENT_WEB_GATE_OWNER") or "admin").strip()
        if owner not in {"admin", "qq"}:
            raise ValueError("COMPANY_AGENT_WEB_GATE_OWNER must be admin or qq")
        return cls(host, port, api_key, search_url, search_timeout, owner)


def make_handler(*, enabled: Callable[[], bool], api_key: str = "",
                 audit: Callable[[str, dict[str, Any]], None] | None = None,
                 search: Callable[[str, int], dict[str, Any]] | None = None,
                 dsh_enabled: Callable[[], bool] | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CompanyAgentWebGate/1.0"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send(self, status: int, value: dict[str, Any]) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not api_key:
                return True
            header_key = self.headers.get("X-Company-Agent-Bridge-Key", "")
            if header_key and hmac.compare_digest(header_key, api_key):
                return True
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                return hmac.compare_digest(auth_header[7:].strip(), api_key)
            return False

        def do_POST(self) -> None:
            route = urlsplit(self.path).path
            if route not in {"/internal/web/authorize", "/internal/web/search", "/internal/web/dsh-search"}:
                self._send(404, {"error": "not found"})
                return
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 16_384:
                    raise ValueError("request body exceeds 16 KiB")
                value = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(value, dict):
                    raise ValueError("request body must be an object")
                is_search = route in {"/internal/web/search", "/internal/web/dsh-search"}
                gate_enabled = dsh_enabled if (route == "/internal/web/dsh-search" and dsh_enabled is not None) else enabled
                operation = "search" if is_search else value.get("operation")
                if operation not in {"search", "fetch"}:
                    raise ValueError("operation must be search or fetch")
                target_key = "query" if operation == "search" else "url"
                target = value.get(target_key)
                if not isinstance(target, str) or not target.strip() or len(target) > 2_000:
                    raise ValueError(f"{target_key} must contain 1-2000 characters")
                request_id = value.get("request_id", "")
                if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 200):
                    raise ValueError("request_id must be at most 200 characters")
                if operation == "search":
                    record = {"operation": operation, "query_sha256": hashlib.sha256(target.strip().encode("utf-8")).hexdigest(),
                              "request_id": request_id or "", "route": route}
                else:
                    parsed_target = urlsplit(target)
                    record = {"operation": operation, "host": (parsed_target.hostname or "")[:255],
                              "request_id": request_id or ""}
                if audit:
                    audit("web_authorize", record)
                if not gate_enabled():
                    self._send(403, {"error": "web search is disabled"})
                    return
                if is_search:
                    if search is None:
                        self._send(503, {"error": "web search backend is unavailable"})
                        return
                    max_results = value.get("max_results", 8)
                    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 8:
                        raise ValueError("max_results must be an integer from 1 to 8")
                    try:
                        result = search(target.strip(), max_results)
                        if audit:
                            audit("web_search_completed", {
                                "request_id": request_id or "",
                                "source_urls": [source["url"] for source in result.get("sources", [])],
                                "source_count": len(result.get("sources", [])),
                            })
                        self._send(200, result)
                    except Exception:
                        if audit:
                            audit("web_search_failed", {"request_id": request_id or "", "error": "backend_failed"})
                        self._send(502, {"error": "web search backend failed"})
                    return
                self._send(200, {
                    "authorized": True,
                    "operation": operation,
                    "authorized_at": datetime.now(timezone.utc).isoformat(),
                })
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                self._send(400, {"error": str(error)[:300]})

    return Handler


class WebGateServer:
    """Small authenticated server that re-checks the Company Agent web switch per call."""

    def __init__(self, settings: WebGateSettings, *, enabled: Callable[[], bool],
                 audit: Callable[[str, dict[str, Any]], None] | None = None,
                 dsh_enabled: Callable[[], bool] | None = None) -> None:
        self.server = ThreadingHTTPServer(
            (settings.host, settings.port),
            make_handler(
                enabled=enabled,
                api_key=settings.api_key,
                audit=audit,
                search=SearxngSearch(settings.search_url, settings.search_timeout_seconds),
                dsh_enabled=dsh_enabled,
            ),
        )
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="company-agent-web-gate", daemon=True)

    @property
    def address(self) -> tuple[str, int]:
        return self.server.server_address

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=3)
