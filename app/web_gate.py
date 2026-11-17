from __future__ import annotations

import hmac
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .runtime_config import RuntimeConfig


@dataclass(frozen=True)
class WebGateSettings:
    """Loopback capability gate used by DSSuperAgent's controlled web tools."""

    host: str = "127.0.0.1"
    port: int = 8767
    api_key: str = ""

    @classmethod
    def load(cls, path: Path) -> "WebGateSettings":
        values = RuntimeConfig._parse_env(path) if path.is_file() else {}
        host = (values.get("COMPANY_AGENT_WEB_GATE_HOST") or "127.0.0.1").strip()
        raw_port = (values.get("COMPANY_AGENT_WEB_GATE_PORT") or "8767").strip()
        try:
            port = int(raw_port)
        except ValueError as error:
            raise ValueError("COMPANY_AGENT_WEB_GATE_PORT must be an integer") from error
        if not 1 <= port <= 65535:
            raise ValueError("COMPANY_AGENT_WEB_GATE_PORT must be between 1 and 65535")
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("COMPANY_AGENT_WEB_GATE_HOST must bind to loopback")
        api_key = (values.get("COMPANY_AGENT_WEB_GATE_API_KEY")
                   or values.get("COMPANY_AGENT_KNOWLEDGE_API_KEY") or "").strip()
        if api_key and len(api_key) < 32:
            raise ValueError("COMPANY_AGENT_WEB_GATE_API_KEY must contain at least 32 characters")
        return cls(host, port, api_key)


def make_handler(*, enabled: Callable[[], bool], api_key: str = "",
                 audit: Callable[[str, dict[str, Any]], None] | None = None):
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
            return not api_key or hmac.compare_digest(
                self.headers.get("X-Company-Agent-Bridge-Key", ""), api_key
            )

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/internal/web/authorize":
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
                operation = value.get("operation")
                if operation not in {"search", "fetch"}:
                    raise ValueError("operation must be search or fetch")
                target_key = "query" if operation == "search" else "url"
                target = value.get(target_key)
                if not isinstance(target, str) or not target.strip() or len(target) > 2_000:
                    raise ValueError(f"{target_key} must contain 1-2000 characters")
                request_id = value.get("request_id", "")
                if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 200):
                    raise ValueError("request_id must be at most 200 characters")
                record = {"operation": operation, target_key: target, "request_id": request_id or ""}
                if audit:
                    audit("web_authorize", record)
                if not enabled():
                    self._send(403, {"error": "web search is disabled"})
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
                 audit: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.server = ThreadingHTTPServer(
            (settings.host, settings.port),
            make_handler(enabled=enabled, api_key=settings.api_key, audit=audit),
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
