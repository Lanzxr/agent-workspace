from __future__ import annotations

import json
import hmac
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .service import KnowledgeAccessError, KnowledgeService


def make_handler(service: KnowledgeService, api_key: str = ""):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CompanyAgentKnowledge/1.0"

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

        def do_POST(self) -> None:
            routes = {
                "/internal/knowledge/search", "/internal/knowledge/read",
                "/internal/attachments/list", "/internal/attachments/read",
                "/internal/conversation/history",
            }
            if self.path not in routes:
                self._send(404, {"error": "not found"})
                return
            if api_key and not hmac.compare_digest(self.headers.get("X-Company-Agent-Bridge-Key", ""), api_key):
                self._send(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 32_768:
                    raise ValueError("request body exceeds 32 KiB")
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict) or not isinstance(value.get("grant"), str):
                    raise ValueError("request must include a scoped grant")
                grant = value["grant"]
                if self.path == "/internal/knowledge/search":
                    if set(value) - {"grant", "query", "limit"}:
                        raise ValueError("invalid knowledge search fields")
                    result = service.search_with_grant(grant, value.get("query"), limit=value.get("limit", 8))
                elif self.path == "/internal/knowledge/read":
                    if set(value) - {"grant", "document_id"}:
                        raise ValueError("invalid knowledge read fields")
                    result = service.read_document_with_grant(grant, value.get("document_id"))
                elif self.path == "/internal/attachments/list":
                    if set(value) - {"grant", "query", "limit"}:
                        raise ValueError("invalid attachment list fields")
                    result = service.list_attachments_with_grant(
                        grant, query=value.get("query", ""), limit=value.get("limit", 30))
                elif self.path == "/internal/attachments/read":
                    if set(value) - {"grant", "attachment_id"}:
                        raise ValueError("invalid attachment read fields")
                    result = service.read_attachment_with_grant(grant, value.get("attachment_id"))
                else:
                    if set(value) - {"grant", "before_event_id", "limit"}:
                        raise ValueError("invalid conversation history fields")
                    result = service.conversation_history_with_grant(
                        grant, before_event_id=value.get("before_event_id"), limit=value.get("limit", 30))
            except KnowledgeAccessError:
                self._send(403, {"error": "grant is invalid, expired, or outside its scope"})
            except (ValueError, TypeError, json.JSONDecodeError):
                self._send(400, {"error": "invalid knowledge search request"})
            except Exception:
                self._send(502, {"error": "knowledge search failed"})
            else:
                self._send(200, result)

    return Handler


class KnowledgeQueryServer:
    """Private capability API used by the DSSuperAgent read-only search tool."""

    def __init__(self, service: KnowledgeService, port: int) -> None:
        self.server = ThreadingHTTPServer((service.settings.query_api_host, port),
                                          make_handler(service, service.settings.query_api_key))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="company-agent-knowledge-api",
                                       daemon=True)

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
