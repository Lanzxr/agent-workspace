from __future__ import annotations

import json
import secrets
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .control_plane import ControlPlane
from .identity import IdentityRegistry
from .knowledge.service import KnowledgeService
from .scheduler import ScheduleService
from .scheduler import SchedulerWorker
from .workspace import WorkspaceError, WorkspaceManager
from .workspace_runtime import WorkspaceRuntime


def make_handler(plane: ControlPlane, data_directory: Path, token: str,
                 knowledge_service: KnowledgeService | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CompanyAgentConsole/1.0"

        def log_message(self, format, *args):
            return

        def _send(self, status: int, value, content_type: str = "application/json; charset=utf-8"):
            body = value.encode("utf-8") if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            return secrets.compare_digest(supplied, f"Bearer {token}")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 64_000:
                raise ValueError("request body exceeds 64 KB")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("request body must be a JSON object")
            return value

        def _route(self, method: str):
            path = urlsplit(self.path).path
            if path == "/" and method == "GET":
                page = Path(__file__).parent.parent / "web" / "index.html"
                if not page.is_file():
                    page = Path(sys.prefix) / "share" / "company-agent" / "web" / "index.html"
                return self._send(200, page.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            if path == "/api/status" and method == "GET":
                rows = plane.store.connection.execute("SELECT status,COUNT(*) AS count FROM work_items GROUP BY status").fetchall()
                qq = plane.get_setting("qq_gateway", {})
                heartbeat = qq.get("updated_at") if isinstance(qq, dict) else None
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds()
                    qq_state = qq.get("state") if age <= 90 else "offline"
                except (TypeError, ValueError):
                    qq_state = "unknown"
                dsh_web_enabled = bool(plane.get_setting("web_search_enabled", False))
                dssuperagent_web_enabled = bool(plane.get_setting("dssuperagent_web_search_enabled", False))
                return self._send(200, {"service": "running", "work_items": {row[0]: row[1] for row in rows},
                                        "qq_gateway": qq_state,
                                        "web_search_enabled": dsh_web_enabled,
                                        "dsh_web_search_enabled": dsh_web_enabled,
                                        "dssuperagent_web_search_enabled": dssuperagent_web_enabled,
                                        "workspace_bytes": _directory_size(data_directory / "workspaces"),
                                        "data_directory": str(data_directory)})
            if path == "/api/knowledge" and method == "GET":
                if knowledge_service is None:
                    return self._send(200, {"configured": False, "documents": {}})
                return self._send(200, {**knowledge_service.summary(),
                                        "items": knowledge_service.list_documents(limit=100)})
            if path == "/api/users" and method == "GET":
                return self._send(200, {"users": [u.__dict__ for u in IdentityRegistry(plane.store).list_users()]})
            if path == "/api/users" and method == "POST":
                body = self._body()
                user = IdentityRegistry(plane.store).create_user(body.get("display_name", ""),
                                                                 time_zone=body.get("time_zone", "UTC"))
                return self._send(201, user.__dict__)
            if path == "/api/bindings" and method == "GET":
                bindings = IdentityRegistry(plane.store).list_bindings()
                return self._send(200, {"bindings": [b.__dict__ for b in bindings]})
            if path == "/api/bindings" and method == "POST":
                body = self._body()
                binding = IdentityRegistry(plane.store).bind(**{key: body[key] for key in
                    ("provider", "account_id", "scope_id", "external_id", "user_id")})
                return self._send(201, binding.__dict__)
            if path == "/api/bindings" and method == "DELETE":
                binding_id = parse_qs(urlsplit(self.path).query).get("binding_id", [""])[0]
                return self._send(200, {"removed": IdentityRegistry(plane.store).unbind(binding_id)})
            if path == "/api/schedules" and method == "GET":
                return self._send(200, {"tasks": [t.__dict__ for t in ScheduleService(plane.store).list()]})
            if path == "/api/schedule-sources" and method == "GET":
                events = plane.store.connection.execute(
                    "SELECT event_id,payload FROM events ORDER BY created_at DESC LIMIT 100").fetchall()
                work_items = plane.store.connection.execute(
                    "SELECT work_item_id,payload,status FROM work_items ORDER BY updated_at DESC LIMIT 100").fetchall()
                return self._send(200, {
                    "events": [{"event_id": row[0], "conversation_id": json.loads(row[1]).get("conversation_id", ""),
                                "content": json.loads(row[1]).get("content", "")[:160]} for row in events],
                    "work_items": [{"work_item_id": row[0], "title": json.loads(row[1]).get("title", row[0]),
                                    "status": row[2]} for row in work_items],
                })
            if path == "/api/schedules" and method == "POST":
                body = self._body()
                task = ScheduleService(plane.store).create(**body)
                return self._send(201, task.__dict__)
            if path == "/api/schedules" and method == "DELETE":
                schedule_id = parse_qs(urlsplit(self.path).query).get("schedule_id", [""])[0]
                return self._send(200, ScheduleService(plane.store).cancel(schedule_id).__dict__)
            if path == "/api/schedules/retry" and method == "POST":
                schedule_id = self._body().get("schedule_id", "")
                return self._send(200, ScheduleService(plane.store).retry(schedule_id).__dict__)
            if path == "/api/settings/web-search" and method == "GET":
                enabled = bool(plane.get_setting("web_search_enabled", False))
                return self._send(200, {"enabled": enabled, "dsh_enabled": enabled,
                                        "dssuperagent_enabled": bool(plane.get_setting("dssuperagent_web_search_enabled", False))})
            if path == "/api/settings/web-search" and method == "PUT":
                enabled = self._body().get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                plane.set_setting("web_search_enabled", enabled)
                return self._send(200, {"enabled": enabled})
            if path in {"/api/settings/dsh-web-search", "/api/settings/dssuperagent-web-search"} and method == "GET":
                key = "web_search_enabled" if path.endswith("dsh-web-search") else "dssuperagent_web_search_enabled"
                return self._send(200, {"enabled": bool(plane.get_setting(key, False))})
            if path in {"/api/settings/dsh-web-search", "/api/settings/dssuperagent-web-search"} and method == "PUT":
                enabled = self._body().get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                key = "web_search_enabled" if path.endswith("dsh-web-search") else "dssuperagent_web_search_enabled"
                plane.set_setting(key, enabled)
                return self._send(200, {"enabled": enabled, "setting": key})
            if path == "/api/logs" and method == "GET":
                rows = plane.store.connection.execute("SELECT entity_type,entity_id,action,payload,created_at "
                    "FROM audit ORDER BY audit_id DESC LIMIT 100").fetchall()
                return self._send(200, {"logs": [{"entity_type": r[0], "entity_id": r[1], "action": r[2],
                    "payload": json.loads(r[3]), "created_at": r[4]} for r in rows]})
            if path == "/api/workspace-logs" and method == "GET":
                query = parse_qs(urlsplit(self.path).query)
                workspace_id = query.get("workspace_id", [""])[0]
                execution_id = query.get("execution_id", [""])[0]
                if not workspace_id or not execution_id:
                    raise ValueError("workspace_id and execution_id are required")
                runtime = WorkspaceRuntime(WorkspaceManager(data_directory / "workspaces"))
                logs = runtime.read_logs(workspace_id, execution_id)
                return self._send(200, logs)
            return self._send(404, {"error": "not found"})

        def do_GET(self): self._safe_route("GET")
        def do_POST(self): self._safe_route("POST")
        def do_PUT(self): self._safe_route("PUT")
        def do_DELETE(self): self._safe_route("DELETE")

        def _safe_route(self, method):
            try:
                self._route(method)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, sqlite3.IntegrityError,
                    WorkspaceError) as error:
                self._send(400, {"error": str(error)[:500]})

    return Handler


def serve(plane: ControlPlane, data_directory: Path, host: str, port: int, token: str,
          scheduler_worker: SchedulerWorker | None = None, *,
          knowledge_service: KnowledgeService | None = None) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("admin console can only bind to loopback; use an authenticated reverse proxy for remote access")
    server = HTTPServer((host, port), make_handler(plane, data_directory, token, knowledge_service))
    stop = threading.Event()
    worker_thread = None
    if scheduler_worker:
        scheduler_worker.schedules.recover_interrupted()
        worker_thread = threading.Thread(target=_run_scheduler, args=(scheduler_worker, stop),
                                         name="company-agent-scheduler", daemon=True)
        worker_thread.start()
    print(f"Company Agent console listening at http://{host}:{port}")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        if worker_thread:
            worker_thread.join(timeout=5)
        server.server_close()
        if scheduler_worker:
            scheduler_worker.schedules.store.close()


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for file in path.rglob("*"):
        try:
            if file.is_symlink():
                continue
            if file.is_file():
                total += file.stat().st_size
        except OSError:
            continue
    return total


def _run_scheduler(worker: SchedulerWorker, stop: threading.Event) -> None:
    while not stop.is_set():
        worker.run_once()
        stop.wait(worker.poll_seconds)
