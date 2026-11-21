from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, urlsplit

from .control_plane import ControlPlane, NotFound
from .identity import IdentityRegistry
from .knowledge.service import KnowledgeService
from .mcp_registry import (
    MCP_ACCESS_LEVELS,
    McpServerSpec,
    describe_mcp_servers,
    merge_mcp_servers,
)
from .observability import audit_label, list_conversations, trace_for_event
from .qq_channel import DEFAULT_AMBIENT_COOLDOWN_SECONDS, DEFAULT_BOT_ALIASES
from .scheduler import ScheduleService
from .scheduler import SchedulerWorker
from .shared_tools import SharedToolsManager
from .workspace import WorkspaceError, WorkspaceManager
from .workspace_retention import (
    MAX_WORKSPACE_RETENTION_DAYS,
    MIN_WORKSPACE_RETENTION_DAYS,
    get_retention_days,
    list_workspace_files,
    prune_expired_workspace_files,
    resolve_workspace_file,
    set_retention_days,
)
from .workspace_runtime import WorkspaceRuntime


def make_handler(plane: ControlPlane, data_directory: Path, token: str,
                 knowledge_service: KnowledgeService | None = None,
                 qq_app_id: str | None = None,
                 mcp_runtime_servers: tuple[McpServerSpec, ...] = (),
                 mcp_secrets: Mapping[str, str] | None = None):
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

        def _send_file(self, resolved: Path, *, download_name: str):
            data = resolved.read_bytes()
            ctype = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
            encoded_name = quote(download_name)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{encoded_name}")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            return secrets.compare_digest(supplied, f"Bearer {token}")

        def _body(self, *, max_bytes: int = 64_000) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= max_bytes:
                raise ValueError(f"request body exceeds {max_bytes} bytes")
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
            if path == "/api/conversations" and method == "GET":
                query = parse_qs(urlsplit(self.path).query)
                conversation_id = query.get("conversation_id", [""])[0].strip()
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError as error:
                    raise ValueError("limit must be an integer") from error
                if conversation_id:
                    conversations = [item for item in list_conversations(plane.store, limit=500)
                                     if item["conversation_id"] == conversation_id]
                    return self._send(200, {"conversation": conversations[0] if conversations else None})
                return self._send(200, {"conversations": list_conversations(plane.store, limit=limit)})
            if path == "/api/message-trace" and method == "GET":
                event_id = parse_qs(urlsplit(self.path).query).get("event_id", [""])[0].strip()
                if not event_id:
                    raise ValueError("event_id is required")
                trace = trace_for_event(plane.store, event_id)
                if trace is None:
                    return self._send(404, {"error": "event not found"})
                return self._send(200, trace)
            if path == "/api/status" and method == "GET":
                rows = plane.store.fetchall("SELECT status,COUNT(*) AS count FROM work_items GROUP BY status")
                run_rows = plane.store.fetchall(
                    "SELECT status,COUNT(*) AS count FROM assistant_runs GROUP BY status")
                execution_rows = plane.store.fetchall(
                    "SELECT status,COUNT(*) AS count FROM executions GROUP BY status")
                active_executions = plane.store.fetchall(
                    "SELECT execution_id,work_item_id,status,updated_at,payload FROM executions "
                    "WHERE status IN ('queued','running') ORDER BY updated_at DESC LIMIT 20")
                qq = plane.get_setting("qq_gateway", {})
                heartbeat = qq.get("updated_at") if isinstance(qq, dict) else None
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds()
                    qq_state = qq.get("state") if age <= 90 else "offline"
                except (TypeError, ValueError):
                    qq_state = "unknown"
                dsh_web_enabled = bool(plane.get_setting("web_search_enabled", False))
                dssuperagent_web_enabled = bool(plane.get_setting("dssuperagent_web_search_enabled", False))
                downward_compat = bool(plane.get_setting("downward_compatibility_enabled", True))
                identities = IdentityRegistry(plane.store)
                users_list = identities.list_users()
                groups_list = identities.list_groups()
                recent_dsh_rows = plane.store.fetchall(
                    "SELECT execution_id,work_item_id,status,updated_at,payload FROM executions "
                    "ORDER BY updated_at DESC LIMIT 20"
                )
                recent_dsh = [{
                    "execution_id": row[0], "work_item_id": row[1],
                    "status": row[2], "updated_at": row[3],
                    "workspace_id": json.loads(row[4]).get("workspace_id"),
                } for row in recent_dsh_rows]
                latest_run_row = plane.store.fetchone(
                    "SELECT MAX(updated_at), COUNT(*) FROM assistant_runs"
                )
                knowledge_summary = (
                    knowledge_service.summary() if knowledge_service is not None
                    else {"configured": False, "documents": {}}
                )
                return self._send(200, {"service": "running", "release_id": os.environ.get("COMPANY_AGENT_RELEASE_ID", "source"),
                                        "work_items": {row[0]: row[1] for row in rows},
                                        "dssuperagent_runs": {row[0]: row[1] for row in run_rows},
                                        "dssuperagent_stats": {
                                            "total": latest_run_row[1] if latest_run_row else 0,
                                            "latest_updated_at": latest_run_row[0] if latest_run_row else None,
                                        },
                                        "dsh_executions": {row[0]: row[1] for row in execution_rows},
                                        "active_dsh": [{"execution_id": row[0], "work_item_id": row[1],
                                                        "status": row[2], "updated_at": row[3],
                                                        "workspace_id": json.loads(row[4]).get("workspace_id")}
                                                       for row in active_executions],
                                        "recent_dsh": recent_dsh,
                                        "qq_gateway": qq_state,
                                        "qq_app_id": qq_app_id or _infer_qq_app_id(plane),
                                        "knowledge_summary": knowledge_summary,
                                        "web_search_enabled": dsh_web_enabled,
                                        "dsh_web_search_enabled": dsh_web_enabled,
                                        "dssuperagent_web_search_enabled": dssuperagent_web_enabled,
                                        "downward_compatibility_enabled": downward_compat,
                                        "user_counts": {
                                            "total": len(users_list),
                                            "admin": sum(1 for u in users_list if u.role == "admin"),
                                            "employee": sum(1 for u in users_list if u.role == "employee"),
                                            "customer": sum(1 for u in users_list if u.role == "customer"),
                                        },
                                        "group_counts": {
                                            "total": len(groups_list),
                                            "internal": sum(1 for g in groups_list if g.tier == "internal"),
                                            "client_group": sum(1 for g in groups_list if g.tier == "client_group"),
                                        },
                                        "open_capability_gaps": len(plane.list_capability_gaps(status="open", limit=300)),
                                        "bot_behavior": _bot_behavior_payload(plane),
                                        "workspace_file_retention_days": get_retention_days(plane),
                                        "workspace_bytes": _directory_size(data_directory / "workspaces"),
                                        "data_directory": str(data_directory)})
            if path == "/api/knowledge" and method == "GET":
                downward_compat = bool(plane.get_setting("downward_compatibility_enabled", True))
                groups_payload = [g.__dict__ for g in IdentityRegistry(plane.store).list_groups()]
                attachments = _recent_attachments(plane)
                if knowledge_service is None:
                    return self._send(200, {"configured": False, "documents": {}, "items": [],
                                            "attachments": attachments,
                                            "downward_compatibility_enabled": downward_compat,
                                            "groups": groups_payload, "shares": []})
                return self._send(200, {**knowledge_service.summary(),
                                        "items": knowledge_service.list_documents(limit=100),
                                        "attachments": attachments,
                                        "downward_compatibility_enabled": downward_compat,
                                        "groups": groups_payload,
                                        "shares": knowledge_service.list_shares()})
            if path == "/api/knowledge/shares" and method == "GET":
                if knowledge_service is None:
                    return self._send(200, {"shares": []})
                target_id = parse_qs(urlsplit(self.path).query).get("target_id", [""])[0].strip() or None
                return self._send(200, {"shares": knowledge_service.list_shares(target_id=target_id)})
            if path == "/api/knowledge/shares" and method == "POST":
                if knowledge_service is None:
                    raise ValueError("knowledge service is not configured")
                body = self._body()
                share = knowledge_service.share_item(
                    target_kind=str(body.get("target_kind") or "document"),
                    target_id=str(body.get("target_id") or ""),
                    target_scope_type=str(body.get("target_scope_type") or ""),
                    target_scope_id=str(body.get("target_scope_id") or ""),
                    created_by=str(body.get("created_by") or "admin"),
                    note=str(body.get("note") or ""),
                )
                return self._send(201, {"share": share})
            if path == "/api/knowledge/shares" and method == "DELETE":
                if knowledge_service is None:
                    raise ValueError("knowledge service is not configured")
                share_id = parse_qs(urlsplit(self.path).query).get("share_id", [""])[0].strip()
                if not share_id:
                    raise ValueError("share_id is required")
                return self._send(200, {"removed": knowledge_service.unshare_item(share_id)})
            if path == "/api/users" and method == "GET":
                identities = IdentityRegistry(plane.store)
                return self._send(200, {
                    "users": [u.__dict__ for u in identities.list_users()],
                    "qq_app_id": qq_app_id or _infer_qq_app_id(plane),
                    "seen_external_actors": _seen_external_actors(plane, identities),
                })
            if path == "/api/users" and method == "POST":
                body = self._body()
                user = IdentityRegistry(plane.store).create_user(
                    body.get("display_name", ""),
                    time_zone=body.get("time_zone", "UTC"),
                    role=body.get("role", "customer"),
                )
                return self._send(201, user.__dict__)
            if path == "/api/users/role" and method == "PUT":
                body = self._body()
                user = IdentityRegistry(plane.store).set_user_role(
                    str(body.get("user_id") or ""),
                    str(body.get("role") or ""),
                )
                return self._send(200, user.__dict__)
            if path == "/api/bind-codes" and method == "POST":
                body = self._body()
                ttl_seconds = int(body.get("ttl_seconds", 86_400))
                code = IdentityRegistry(plane.store).create_bind_code(
                    str(body.get("user_id") or ""), ttl_seconds=ttl_seconds,
                )
                return self._send(201, code)
            if path == "/api/groups" and method == "GET":
                return self._send(200, {"groups": [g.__dict__ for g in IdentityRegistry(plane.store).list_groups()]})
            if path == "/api/groups" and method == "PUT":
                body = self._body()
                group = IdentityRegistry(plane.store).update_group(
                    source_type=str(body.get("source_type") or "qq.group"),
                    source_id=str(body.get("source_id") or ""),
                    tier=body.get("tier"),
                    downward_compat=body.get("downward_compat"),
                    display_name=body.get("display_name"),
                    notes=body.get("notes"),
                )
                return self._send(200, {"group": group.__dict__})
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
                events = plane.store.fetchall(
                    "SELECT event_id,payload FROM events ORDER BY created_at DESC LIMIT 100")
                work_items = plane.store.fetchall(
                    "SELECT work_item_id,payload,status FROM work_items ORDER BY updated_at DESC LIMIT 100")
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
            if path == "/api/settings/downward-compat" and method == "GET":
                return self._send(200, {"enabled": bool(plane.get_setting("downward_compatibility_enabled", True))})
            if path == "/api/settings/downward-compat" and method == "PUT":
                enabled = self._body().get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                plane.set_setting("downward_compatibility_enabled", enabled)
                return self._send(200, {"enabled": enabled, "setting": "downward_compatibility_enabled"})
            if path == "/api/settings/bot-behavior" and method == "GET":
                return self._send(200, _bot_behavior_payload(plane))
            if path == "/api/settings/bot-behavior" and method == "PUT":
                body = self._body()
                if "ambient_listen_enabled" in body:
                    if not isinstance(body["ambient_listen_enabled"], bool):
                        raise ValueError("ambient_listen_enabled must be a boolean")
                    plane.set_setting("ambient_listen_enabled", body["ambient_listen_enabled"])
                if "ambient_cooldown_seconds" in body:
                    try:
                        cooldown = float(body["ambient_cooldown_seconds"])
                    except (TypeError, ValueError) as error:
                        raise ValueError("ambient_cooldown_seconds must be a number") from error
                    if not 0 <= cooldown <= 3600:
                        raise ValueError("ambient_cooldown_seconds must be between 0 and 3600")
                    plane.set_setting("ambient_cooldown_seconds", cooldown)
                if "bot_aliases" in body:
                    raw_aliases = body["bot_aliases"]
                    if isinstance(raw_aliases, str):
                        aliases = [a.strip() for a in raw_aliases.replace("，", ",").split(",") if a.strip()]
                    elif isinstance(raw_aliases, list):
                        aliases = [str(a).strip() for a in raw_aliases if isinstance(a, str) and a.strip()]
                    else:
                        raise ValueError("bot_aliases must be a list of strings")
                    deduped = list(dict.fromkeys(item[:40] for item in aliases[:30]))
                    plane.set_setting("bot_aliases", deduped)
                return self._send(200, _bot_behavior_payload(plane))
            if path == "/api/capability-gaps" and method == "GET":
                query = parse_qs(urlsplit(self.path).query)
                status_filter = query.get("status", [""])[0].strip() or None
                try:
                    limit = max(1, min(int(query.get("limit", ["150"])[0]), 300))
                except ValueError as error:
                    raise ValueError("limit must be an integer") from error
                groups_by_key = {g.scope_key: g for g in IdentityRegistry(plane.store).list_groups()}
                gaps = plane.list_capability_gaps(status=status_filter, limit=limit)
                enriched = []
                for item in gaps:
                    grp = groups_by_key.get(f"{item['source_type']}:{item['source_id']}")
                    enriched.append({
                        **item,
                        "group_display_name": grp.display_name if grp else "",
                        "tier_label": "内部群 L2" if item["group_tier"] == "internal" else (
                            "管理员私聊 L3" if item["group_tier"] == "admin" else "客户层 L1"
                        ),
                    })
                open_count = len(plane.list_capability_gaps(status="open", limit=300))
                return self._send(200, {"gaps": enriched, "open_count": open_count})
            if path == "/api/capability-gaps" and method == "PUT":
                body = self._body()
                updated = plane.update_capability_gap_status(
                    str(body.get("gap_id") or ""),
                    status=str(body.get("status") or ""),
                    resolution_note=str(body.get("resolution_note") or ""),
                )
                return self._send(200, {"gap": updated})
            if path == "/api/tools" and method == "GET":
                return self._send(200, SharedToolsManager(data_directory / "shared-tools").summary())
            if path == "/api/tools/script" and method == "GET":
                query = parse_qs(urlsplit(self.path).query)
                layer = query.get("layer", [""])[0].strip()
                name = query.get("name", [""])[0].strip()
                return self._send(200, SharedToolsManager(data_directory / "shared-tools").read_script(layer, name))
            if path == "/api/tools/scripts" and method in {"POST", "PUT"}:
                body = self._body(max_bytes=300_000)
                saved = SharedToolsManager(data_directory / "shared-tools").save_script(
                    str(body.get("layer") or ""),
                    str(body.get("name") or ""),
                    str(body.get("content") or ""),
                    description=str(body.get("description") or ""),
                )
                return self._send(200, {"script": saved})
            if path == "/api/tools/scripts" and method == "DELETE":
                query = parse_qs(urlsplit(self.path).query)
                layer = query.get("layer", [""])[0].strip()
                name = query.get("name", [""])[0].strip()
                return self._send(200, {"removed": SharedToolsManager(data_directory / "shared-tools").delete_script(layer, name)})
            if path == "/api/tools/install" and method == "POST":
                body = self._body()
                packages = body.get("packages")
                if isinstance(packages, str):
                    packages = [item for item in packages.replace(",", " ").split() if item.strip()]
                installed = SharedToolsManager(data_directory / "shared-tools").install_python_packages(
                    str(body.get("layer") or ""),
                    packages if isinstance(packages, list) else [],
                )
                return self._send(200, installed)
            if path == "/api/mcp-servers" and method == "GET":
                servers = describe_mcp_servers(
                    mcp_runtime_servers, plane.get_setting("mcp_servers", []), mcp_secrets,
                )
                return self._send(200, {"servers": servers, "access_levels": list(MCP_ACCESS_LEVELS)})
            if path == "/api/mcp-servers" and method in {"POST", "PUT"}:
                body = self._body(max_bytes=128_000)
                saved = _upsert_mcp_override(plane, mcp_runtime_servers, body)
                servers = describe_mcp_servers(
                    mcp_runtime_servers, plane.get_setting("mcp_servers", []), mcp_secrets,
                )
                return self._send(200, {"server": saved, "servers": servers})
            if path == "/api/mcp-servers" and method == "DELETE":
                query = parse_qs(urlsplit(self.path).query)
                server_id = query.get("server_id", [""])[0].strip()
                removed = _delete_mcp_override(plane, server_id)
                servers = describe_mcp_servers(
                    mcp_runtime_servers, plane.get_setting("mcp_servers", []), mcp_secrets,
                )
                return self._send(200, {"removed": removed, "servers": servers})
            if path == "/api/logs" and method == "GET":
                query = parse_qs(urlsplit(self.path).query)
                entity_type = query.get("entity_type", [""])[0].strip()
                try:
                    limit = max(1, min(int(query.get("limit", ["120"])[0]), 300))
                except ValueError as error:
                    raise ValueError("limit must be an integer") from error
                if entity_type:
                    rows = plane.store.fetchall(
                        "SELECT entity_type,entity_id,action,payload,created_at "
                        "FROM audit WHERE entity_type=? ORDER BY audit_id DESC LIMIT ?",
                        (entity_type, limit),
                    )
                else:
                    rows = plane.store.fetchall(
                        "SELECT entity_type,entity_id,action,payload,created_at "
                        "FROM audit ORDER BY audit_id DESC LIMIT ?",
                        (limit,),
                    )
                return self._send(200, {"logs": [{
                    "entity_type": r[0], "entity_id": r[1], "action": r[2],
                    "label": audit_label(r[0], r[2], r[1]),
                    "payload": json.loads(r[3]), "created_at": r[4],
                } for r in rows]})
            if path == "/api/workspace-logs" and method == "GET":
                query = parse_qs(urlsplit(self.path).query)
                workspace_id = query.get("workspace_id", [""])[0]
                execution_id = query.get("execution_id", [""])[0]
                if not workspace_id or not execution_id:
                    raise ValueError("workspace_id and execution_id are required")
                runtime = WorkspaceRuntime(WorkspaceManager(data_directory / "workspaces"))
                logs = runtime.read_logs(workspace_id, execution_id)
                return self._send(200, logs)
            if path == "/api/settings/file-retention" and method == "GET":
                return self._send(200, {
                    "retention_days": get_retention_days(plane),
                    "min_days": MIN_WORKSPACE_RETENTION_DAYS,
                    "max_days": MAX_WORKSPACE_RETENTION_DAYS,
                })
            if path == "/api/settings/file-retention" and method == "PUT":
                body = self._body()
                days = set_retention_days(plane, body.get("retention_days"))
                return self._send(200, {
                    "retention_days": days,
                    "min_days": MIN_WORKSPACE_RETENTION_DAYS,
                    "max_days": MAX_WORKSPACE_RETENTION_DAYS,
                })
            if path == "/api/workspace-files" and method == "GET":
                query = parse_qs(urlsplit(self.path).query)
                ws_filter = query.get("workspace_id", [""])[0].strip() or None
                try:
                    limit = max(1, min(int(query.get("limit", ["200"])[0]), 500))
                except ValueError as error:
                    raise ValueError("limit must be an integer") from error
                days = get_retention_days(plane)
                files = list_workspace_files(
                    WorkspaceManager(data_directory / "workspaces"),
                    workspace_id=ws_filter,
                    retention_days=days,
                    limit=limit,
                )
                return self._send(200, {"retention_days": days, "files": files})
            if path == "/api/workspace-files/download" and method == "GET":
                query = parse_qs(urlsplit(self.path).query)
                ws_id = query.get("workspace_id", [""])[0].strip()
                rel_path = query.get("path", [""])[0].strip()
                if not ws_id or not rel_path:
                    raise ValueError("workspace_id and path are required")
                resolved = resolve_workspace_file(
                    WorkspaceManager(data_directory / "workspaces"), ws_id, rel_path,
                )
                if resolved.stat().st_size > 50 * 1024 * 1024:
                    raise ValueError("file exceeds 50 MiB download limit")
                return self._send_file(resolved, download_name=resolved.name)
            if path == "/api/workspace-files/prune" and method == "POST":
                body = self._body()
                ws_filter = str(body.get("workspace_id") or "").strip() or None
                days = get_retention_days(plane)
                summary = prune_expired_workspace_files(
                    WorkspaceManager(data_directory / "workspaces"),
                    retention_days=days,
                    workspace_id=ws_filter,
                )
                plane.record_external_audit("workspace_files_pruned", summary)
                return self._send(200, summary)
            return self._send(404, {"error": "not found"})

        def do_GET(self): self._safe_route("GET")
        def do_POST(self): self._safe_route("POST")
        def do_PUT(self): self._safe_route("PUT")
        def do_DELETE(self): self._safe_route("DELETE")

        def _safe_route(self, method):
            try:
                self._route(method)
            except (FileNotFoundError, NotFound) as error:
                self._send(404, {"error": str(error)[:300]})
            except RuntimeError as error:
                self._send(502, {"error": str(error)[:500]})
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, sqlite3.IntegrityError,
                    WorkspaceError) as error:
                self._send(400, {"error": str(error)[:500]})

    return Handler


def serve(plane: ControlPlane, data_directory: Path, host: str, port: int, token: str,
          scheduler_worker: SchedulerWorker | None = None, *,
          knowledge_service: KnowledgeService | None = None,
          qq_app_id: str | None = None,
          mcp_runtime_servers: tuple[McpServerSpec, ...] = (),
          mcp_secrets: Mapping[str, str] | None = None) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("admin console can only bind to loopback; use an authenticated reverse proxy for remote access")
    server = HTTPServer(
        (host, port),
        make_handler(
            plane, data_directory, token, knowledge_service,
            qq_app_id=qq_app_id,
            mcp_runtime_servers=mcp_runtime_servers,
            mcp_secrets=mcp_secrets,
        ),
    )
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


def _bot_behavior_payload(plane: ControlPlane) -> dict[str, object]:
    raw_aliases = plane.get_setting("bot_aliases", None)
    aliases = (
        [str(item).strip() for item in raw_aliases if isinstance(item, str) and item.strip()]
        if isinstance(raw_aliases, list)
        else list(DEFAULT_BOT_ALIASES)
    )
    if not aliases:
        aliases = list(DEFAULT_BOT_ALIASES)
    raw_cd = plane.get_setting("ambient_cooldown_seconds", DEFAULT_AMBIENT_COOLDOWN_SECONDS)
    try:
        cooldown = float(raw_cd)
    except (TypeError, ValueError):
        cooldown = DEFAULT_AMBIENT_COOLDOWN_SECONDS
    return {
        "ambient_listen_enabled": bool(plane.get_setting("ambient_listen_enabled", False)),
        "ambient_cooldown_seconds": cooldown,
        "bot_aliases": aliases,
    }


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


def _infer_qq_app_id(plane: ControlPlane) -> str:
    row = plane.store.fetchone(
        "SELECT event_id FROM events WHERE event_id LIKE 'qq:%' ORDER BY created_at DESC LIMIT 1"
    )
    if row is None:
        return ""
    parts = str(row[0]).split(":", 3)
    return parts[1] if len(parts) >= 3 and parts[0] == "qq" else ""


def _seen_external_actors(plane: ControlPlane, identities: IdentityRegistry) -> list[dict[str, str]]:
    users = {u.user_id: u for u in identities.list_users()}
    rows = plane.store.fetchall(
        "SELECT payload,created_at FROM events ORDER BY created_at DESC LIMIT 200"
    )
    seen: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in rows:
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            continue
        source_type = str(payload.get("source_type") or "")
        if not source_type.startswith("qq."):
            continue
        event_id = str(payload.get("event_id") or "")
        parts = event_id.split(":", 3)
        account_id = parts[1] if len(parts) >= 3 and parts[0] == "qq" else ""
        external_id = str(payload.get("external_actor_id") or "")
        if not external_id or not account_id:
            continue
        source_id = str(payload.get("source_id") or "")
        scope_id = source_id if source_type == "qq.group" else "*"
        key = (account_id, scope_id, external_id)
        if key in seen:
            continue
        resolved = identities.resolve(
            provider="qq", account_id=account_id, scope_id=scope_id, external_id=external_id,
        )
        matched_user = users.get(resolved)
        seen[key] = {
            "provider": "qq",
            "account_id": account_id,
            "scope_id": scope_id,
            "source_type": source_type,
            "source_id": source_id,
            "external_id": external_id,
            "resolved_user_id": matched_user.user_id if matched_user else "",
            "resolved_name": matched_user.display_name if matched_user else "",
            "resolved_role": matched_user.role if matched_user else "",
            "last_seen_at": str(payload.get("occurred_at") or row[1]),
        }
        if len(seen) >= 40:
            break
    return list(seen.values())


def _recent_attachments(plane: ControlPlane, limit: int = 50) -> list[dict[str, object]]:
    rows = plane.store.fetchall(
        "SELECT payload FROM attachments ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    result: list[dict[str, object]] = []
    for row in rows:
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            continue
        result.append({
            "attachment_id": payload.get("attachment_id", ""),
            "event_id": payload.get("event_id", ""),
            "filename": payload.get("filename", ""),
            "content_type": payload.get("content_type", ""),
            "status": payload.get("status", ""),
            "size_bytes": payload.get("size_bytes", 0),
            "scope_type": payload.get("scope_type", ""),
            "scope_id": payload.get("scope_id", ""),
            "document_id": payload.get("document_id"),
            "sensitive": bool(payload.get("sensitive")),
            "received_at": payload.get("received_at", ""),
        })
    return result


def _run_scheduler(worker: SchedulerWorker, stop: threading.Event) -> None:
    while not stop.is_set():
        worker.run_once()
        stop.wait(worker.poll_seconds)


def _upsert_mcp_override(
    plane: ControlPlane,
    runtime_servers: tuple[McpServerSpec, ...],
    body: Mapping[str, Any],
) -> dict[str, Any]:
    raw_id = str(body.get("server_id") or body.get("id") or "").strip()
    if not raw_id:
        raise ValueError("server_id is required")
    runtime_ids = {spec.server_id for spec in runtime_servers}
    raw_existing = plane.get_setting("mcp_servers", [])
    stored = [dict(item) for item in raw_existing if isinstance(item, Mapping)] if isinstance(raw_existing, list) else []
    stored = [item for item in stored if str(item.get("server_id") or item.get("id") or "").strip() != raw_id]
    if raw_id in runtime_ids and "transport" not in body:
        merged = merge_mcp_servers(runtime_servers, [*stored, body])
        target_spec = next(spec for spec in merged if spec.server_id == raw_id)
        entry: dict[str, Any] = {"server_id": raw_id}
        for key in ("enabled", "access_level", "description", "tool_call_timeout_ms", "fail_on_startup_error"):
            if key in body:
                entry[key] = getattr(target_spec, key)
    else:
        target_spec = McpServerSpec.from_dict(body)
        entry = {
            key: value for key, value in target_spec.to_dict(source="console").items()
            if key not in {"source", "referenced_secret_vars", "missing_secret_vars"}
        }
    stored.append(entry)
    plane.set_setting("mcp_servers", stored)
    plane.record_external_audit("mcp_server_saved", {
        "server_id": target_spec.server_id,
        "transport": target_spec.transport,
        "access_level": target_spec.access_level,
        "enabled": target_spec.enabled,
    })
    return target_spec.to_dict(source="runtime+override" if raw_id in runtime_ids else "console")


def _delete_mcp_override(plane: ControlPlane, server_id: str) -> bool:
    if not server_id:
        raise ValueError("server_id is required")
    raw_existing = plane.get_setting("mcp_servers", [])
    stored = [dict(item) for item in raw_existing if isinstance(item, Mapping)] if isinstance(raw_existing, list) else []
    filtered = [item for item in stored if str(item.get("server_id") or item.get("id") or "").strip() != server_id]
    if len(filtered) == len(stored):
        raise NotFound(f"MCP console override or entry not found: {server_id}")
    plane.set_setting("mcp_servers", filtered)
    plane.record_external_audit("mcp_server_deleted", {"server_id": server_id})
    return True
