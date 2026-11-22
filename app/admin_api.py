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
from .models import source_identity_scope, source_provider
from .observability import audit_label, list_conversations, trace_for_event
from .ports import assert_port_available
from .channel_core import DEFAULT_AMBIENT_COOLDOWN_SECONDS, DEFAULT_BOT_ALIASES
from .qq_channel import QQBotAPI
from .runtime_config import (
    DEFAULT_MODEL_CONTEXT_WINDOW,
    DEFAULT_MODEL_MAX_TOKENS,
    QQBotSettings,
    RuntimeConfig,
    VALID_REASONING_EFFORTS,
    WECOM_DEFAULT_WS_URL,
    WeComBotSettings,
    mask_secret,
    update_runtime_env_file,
)
from .scheduler import ScheduleService
from .scheduler import SchedulerWorker
from .shared_tools import SharedToolsManager
from .tool_forge import ForgeSecretsVault, ToolForgeError, ToolForgeSecurityError, ToolForgeService
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
                 wecom_bot_id: str | None = None,
                 mcp_runtime_servers: tuple[McpServerSpec, ...] = (),
                 mcp_secrets: Mapping[str, str] | None = None,
                 tool_forge: ToolForgeService | None = None,
                 forge_secrets_vault: ForgeSecretsVault | None = None,
                 config_path: Path | None = None):
    resolved_config_path = (config_path or (Path.home() / ".config/company-agent/runtime.env")).expanduser().resolve()
    shared_tools_mgr = SharedToolsManager(data_directory / "shared-tools")
    forge_vault = forge_secrets_vault or ForgeSecretsVault(data_directory / "forge-secrets.json")
    forge_service = tool_forge or ToolForgeService(plane, secrets_vault=forge_vault, shared_tools=shared_tools_mgr)

    def _route_forge(method: str, path: str, query: dict[str, list[str]], body_fn) -> tuple[int, Any] | None:
        if path == "/api/forge/summary" and method == "GET":
            return 200, forge_service.summary()
        if path == "/api/forge/domains" and method == "GET":
            return 200, {"domains": forge_service.list_domains()}
        if path == "/api/forge/domains" and method == "POST":
            body = body_fn()
            return 201, forge_service.add_domain(
                str(body.get("domain") or ""),
                allow_subdomains=bool(body.get("allow_subdomains", False)),
                note=str(body.get("note") or ""),
                created_by="console",
            )
        if path == "/api/forge/domains" and method == "DELETE":
            domain = (query.get("domain") or [""])[0].strip()
            return 200, {"deleted": forge_service.remove_domain(domain, actor="console")}
        if path == "/api/forge/secrets" and method == "GET":
            return 200, {"secrets": forge_service.list_secrets_metadata()}
        if path == "/api/forge/secrets" and method == "POST":
            body = body_fn()
            return 201, forge_service.set_secret(
                str(body.get("name") or ""),
                str(body.get("value") or ""),
                actor="console",
            )
        if path == "/api/forge/secrets" and method == "DELETE":
            name = (query.get("name") or [""])[0].strip()
            return 200, {"deleted": forge_service.delete_secret(name, actor="console")}
        if path == "/api/forge/tools" and method == "GET":
            return 200, {"tools": forge_service.list_tools()}
        if path == "/api/forge/tool" and method == "GET":
            tool_id = (query.get("tool_id") or [""])[0].strip()
            ver_raw = (query.get("version") or [""])[0].strip()
            return 200, forge_service.get_tool(tool_id, version=int(ver_raw) if ver_raw else None)
        if path == "/api/forge/tools/draft" and method == "POST":
            body = body_fn()
            return 201, forge_service.draft_tool(
                tool_id=str(body.get("tool_id") or ""),
                kind=str(body.get("kind") or ""),
                description=str(body.get("description") or ""),
                when_to_use=str(body.get("when_to_use") or ""),
                when_not_to_use=str(body.get("when_not_to_use") or ""),
                operation_type=str(body.get("operation_type") or "read"),
                target_visibility=str(body.get("target_visibility") or "internal"),
                input_schema=body.get("input_schema"),
                http_spec=body.get("http_spec"),
                python_code=str(body.get("python_code") or ""),
                script_name=str(body.get("script_name") or ""),
                script_content=str(body.get("script_content") or ""),
                allowed_domains=body.get("allowed_domains"),
                default_test_input=body.get("default_test_input"),
                created_by="console",
            )
        if path == "/api/forge/tools/test" and method == "POST":
            body = body_fn()
            ver = body.get("version")
            return 200, forge_service.test_tool(
                str(body.get("tool_id") or ""),
                version=int(ver) if ver is not None else None,
                arguments=body.get("arguments"),
            )
        if path == "/api/forge/tools/submit" and method == "POST":
            body = body_fn()
            ver = body.get("version")
            return 201, forge_service.submit_release(
                str(body.get("tool_id") or ""),
                version=int(ver) if ver is not None else None,
                target_visibility=body.get("target_visibility"),
                requested_by="console",
                summary=str(body.get("summary") or ""),
            )
        if path == "/api/forge/tools/enable" and method == "PUT":
            body = body_fn()
            return 200, forge_service.set_tool_enabled(
                str(body.get("tool_id") or ""),
                enabled=bool(body.get("enabled")),
                actor="console",
            )
        if path == "/api/forge/tools/rollback" and method == "POST":
            body = body_fn()
            ver = body.get("target_version")
            return 200, forge_service.rollback_tool(
                str(body.get("tool_id") or ""),
                target_version=int(ver) if ver is not None else None,
                actor="console",
            )
        if path == "/api/forge/proposals" and method == "GET":
            status = (query.get("status") or ["all"])[0].strip() or "all"
            limit_raw = (query.get("limit") or ["100"])[0].strip()
            return 200, {"proposals": forge_service.list_proposals(status=status, limit=int(limit_raw))}
        if path == "/api/forge/proposals/approve" and method == "POST":
            body = body_fn()
            return 200, forge_service.approve_proposal(
                str(body.get("proposal_id") or ""),
                decided_by="console",
                is_admin=True,
            )
        if path == "/api/forge/proposals/reject" and method == "POST":
            body = body_fn()
            return 200, forge_service.reject_proposal(
                str(body.get("proposal_id") or ""),
                decided_by="console",
                is_admin=True,
                reason=str(body.get("reason") or ""),
            )
        return None
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
                resolved_qq_id = qq_app_id or _infer_channel_account_id(plane, "qq")
                resolved_wecom_id = wecom_bot_id or _infer_channel_account_id(plane, "wecom")
                qq_state = _gateway_state(plane, "qq_gateway", configured=bool(qq_app_id) if qq_app_id is not None else None)
                wecom_state = _gateway_state(
                    plane,
                    "wecom_gateway",
                    configured=bool(wecom_bot_id) if wecom_bot_id is not None else bool(resolved_wecom_id),
                )
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
                                        "qq_app_id": resolved_qq_id,
                                        "wecom_gateway": wecom_state,
                                        "wecom_bot_id": resolved_wecom_id,
                                        "channels": {
                                            "qq": {"state": qq_state, "account_id": resolved_qq_id},
                                            "wecom": {"state": wecom_state, "account_id": resolved_wecom_id},
                                        },
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
                    "qq_app_id": qq_app_id or _infer_channel_account_id(plane, "qq"),
                    "wecom_bot_id": wecom_bot_id or _infer_channel_account_id(plane, "wecom"),
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
            if path == "/api/settings/runtime-config" and method == "GET":
                return self._send(200, _runtime_config_payload(plane, resolved_config_path))
            if path == "/api/settings/model-config" and method == "PUT":
                body = self._body()
                payload = _save_model_config(plane, resolved_config_path, body)
                return self._send(200, payload)
            if path == "/api/settings/model-config/test" and method == "POST":
                body = self._body()
                result = _test_model_connection(resolved_config_path, body)
                return self._send(200, result)
            if path == "/api/settings/channel-config" and method == "PUT":
                body = self._body()
                payload = _save_channel_config(plane, resolved_config_path, body)
                return self._send(200, payload)
            if path == "/api/settings/channel-config/test" and method == "POST":
                body = self._body()
                result = _test_channel_connection(resolved_config_path, body)
                return self._send(200, result)
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
            if path.startswith("/api/forge/"):
                forge_query = parse_qs(urlsplit(self.path).query)
                res = _route_forge(method, path, forge_query, self._body)
                if res is not None:
                    return self._send(res[0], res[1])
            return self._send(404, {"error": "not found"})

        def do_GET(self): self._safe_route("GET")
        def do_POST(self): self._safe_route("POST")
        def do_PUT(self): self._safe_route("PUT")
        def do_DELETE(self): self._safe_route("DELETE")

        def _safe_route(self, method):
            try:
                self._route(method)
            except ToolForgeSecurityError as error:
                self._send(403, {"error": str(error)[:400]})
            except (FileNotFoundError, NotFound) as error:
                self._send(404, {"error": str(error)[:300]})
            except RuntimeError as error:
                self._send(502, {"error": str(error)[:500]})
            except (ToolForgeError, ValueError, KeyError, TypeError, json.JSONDecodeError, sqlite3.IntegrityError,
                    WorkspaceError) as error:
                self._send(400, {"error": str(error)[:500]})

    return Handler


def serve(plane: ControlPlane, data_directory: Path, host: str, port: int, token: str,
          scheduler_worker: SchedulerWorker | None = None, *,
          knowledge_service: KnowledgeService | None = None,
          qq_app_id: str | None = None,
          wecom_bot_id: str | None = None,
          mcp_runtime_servers: tuple[McpServerSpec, ...] = (),
          mcp_secrets: Mapping[str, str] | None = None,
          tool_forge: ToolForgeService | None = None,
          forge_secrets_vault: ForgeSecretsVault | None = None,
          config_path: Path | None = None) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("admin console can only bind to loopback; use an authenticated reverse proxy for remote access")
    assert_port_available(host, port, service_name="Admin Console", env_var="COMPANY_AGENT_ADMIN_PORT")
    server = HTTPServer(
        (host, port),
        make_handler(
            plane, data_directory, token, knowledge_service,
            qq_app_id=qq_app_id,
            wecom_bot_id=wecom_bot_id,
            mcp_runtime_servers=mcp_runtime_servers,
            mcp_secrets=mcp_secrets,
            tool_forge=tool_forge,
            forge_secrets_vault=forge_secrets_vault,
            config_path=config_path,
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


def _gateway_state(plane: ControlPlane, setting_key: str, *, configured: bool | None = None) -> str:
    raw = plane.get_setting(setting_key, {})
    heartbeat = raw.get("updated_at") if isinstance(raw, dict) else None
    if not heartbeat:
        if configured is False:
            return "disabled"
        return "unknown"
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds()
        return str(raw.get("state") or "unknown") if age <= 90 else "offline"
    except (TypeError, ValueError):
        return "unknown"


def _infer_channel_account_id(plane: ControlPlane, prefix: str) -> str:
    row = plane.store.fetchone(
        "SELECT event_id FROM events WHERE event_id LIKE ? ORDER BY created_at DESC LIMIT 1",
        (f"{prefix}:%",),
    )
    if row is None:
        return ""
    parts = str(row[0]).split(":", 3)
    return parts[1] if len(parts) >= 3 and parts[0] == prefix else ""


def _infer_qq_app_id(plane: ControlPlane) -> str:
    return _infer_channel_account_id(plane, "qq")


def _seen_external_actors(plane: ControlPlane, identities: IdentityRegistry) -> list[dict[str, str]]:
    users = {u.user_id: u for u in identities.list_users()}
    rows = plane.store.fetchall(
        "SELECT payload,created_at FROM events ORDER BY created_at DESC LIMIT 200"
    )
    seen: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for row in rows:
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            continue
        source_type = str(payload.get("source_type") or "")
        provider = source_provider(source_type)
        if not provider:
            continue
        event_id = str(payload.get("event_id") or "")
        parts = event_id.split(":", 3)
        account_id = parts[1] if len(parts) >= 3 and parts[0] == provider else ""
        external_id = str(payload.get("external_actor_id") or "")
        if not external_id or not account_id:
            continue
        source_id = str(payload.get("source_id") or "")
        scope_id = source_identity_scope(source_type, source_id)
        key = (provider, account_id, scope_id, external_id)
        if key in seen:
            continue
        resolved = identities.resolve(
            provider=provider, account_id=account_id, scope_id=scope_id, external_id=external_id,
        )
        matched_user = users.get(resolved)
        seen[key] = {
            "provider": provider,
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


def _load_env_safe(config_path: Path) -> dict[str, str]:
    if not config_path.is_file():
        return {}
    try:
        return RuntimeConfig._parse_env(config_path)
    except ValueError:
        return {}


def _runtime_config_payload(plane: ControlPlane, config_path: Path) -> dict[str, Any]:
    values = _load_env_safe(config_path)
    primary_url = (values.get("AGENT_MODEL_URL") or values.get("DEEPSEEK_BASE_URL") or "").strip().rstrip("/")
    primary_name = (values.get("AGENT_MODEL_NAME") or "").strip()
    primary_key = (values.get("AGENT_MODEL_API_KEY") or values.get("DEEPSEEK_API_KEY") or "").strip()
    try:
        ctx_win = int((values.get("AGENT_MODEL_CONTEXT_WINDOW") or "").strip() or DEFAULT_MODEL_CONTEXT_WINDOW)
    except ValueError:
        ctx_win = DEFAULT_MODEL_CONTEXT_WINDOW
    try:
        max_tok = int((values.get("AGENT_MODEL_MAX_TOKENS") or "").strip() or min(DEFAULT_MODEL_MAX_TOKENS, ctx_win))
    except ValueError:
        max_tok = min(DEFAULT_MODEL_MAX_TOKENS, ctx_win)
    reasoning = (values.get("AGENT_MODEL_REASONING_EFFORT") or "").strip().lower()
    fb_name = (values.get("AGENT_MODEL_FALLBACK_NAME") or "").strip()
    fb_url = (values.get("AGENT_MODEL_FALLBACK_URL") or "").strip().rstrip("/")
    fb_key = (values.get("AGENT_MODEL_FALLBACK_API_KEY") or "").strip()

    qq_app = (values.get("QQBOT_APP_ID") or values.get("QQBOT_APPID") or "").strip()
    qq_secret = (values.get("QQBOT_CLIENT_SECRET") or values.get("QQBOT_SECRET") or "").strip()
    qq_base_url = (values.get("QQBOT_API_BASE_URL") or "https://api.bot.qq.com").strip().rstrip("/")
    qq_pubs = (values.get("QQBOT_ALLOWED_PUBLICATIONS") or "group_update").strip()
    qq_auto = (values.get("QQBOT_AUTO_EXECUTE_CAPABILITIES") or "dsh.task").strip()
    qq_ws = (values.get("COMPANY_AGENT_QQ_WORKSPACE") or "qq-default").strip()
    qq_iso = (values.get("COMPANY_AGENT_QQ_ISOLATE_WORKSPACES") or "true").strip().lower() in {"1", "true", "yes", "on"}
    qq_configured = bool(qq_app and qq_secret)

    wc_id = (values.get("WECOM_BOT_ID") or values.get("COMPANY_AGENT_WECOM_BOT_ID") or "").strip()
    wc_secret = (values.get("WECOM_BOT_SECRET") or values.get("COMPANY_AGENT_WECOM_BOT_SECRET") or "").strip()
    wc_ws_url = (values.get("WECOM_WS_URL") or WECOM_DEFAULT_WS_URL).strip()
    wc_pubs = (values.get("WECOM_ALLOWED_PUBLICATIONS") or "group_update").strip()
    wc_auto = (values.get("WECOM_AUTO_EXECUTE_CAPABILITIES") or "dsh.task").strip()
    wc_ws = (values.get("COMPANY_AGENT_WECOM_WORKSPACE") or "wecom-default").strip()
    wc_iso = (values.get("COMPANY_AGENT_WECOM_ISOLATE_WORKSPACES") or "true").strip().lower() in {"1", "true", "yes", "on"}
    wc_direct = (values.get("WECOM_GROUP_DIRECT_ON_CALLBACK") or "true").strip().lower() in {"1", "true", "yes", "on"}
    wc_configured = bool(wc_id and wc_secret)

    return {
        "model": {
            "url": primary_url,
            "name": primary_name,
            "api_key_configured": bool(primary_key),
            "api_key_masked": mask_secret(primary_key),
            "context_window": ctx_win,
            "max_tokens": max_tok,
            "reasoning_effort": reasoning,
            "fallback_name": fb_name,
            "fallback_url": fb_url,
            "fallback_api_key_configured": bool(fb_key),
            "fallback_api_key_masked": mask_secret(fb_key),
        },
        "channels": {
            "qq": {
                "configured": qq_configured,
                "app_id": qq_app,
                "client_secret_configured": bool(qq_secret),
                "client_secret_masked": mask_secret(qq_secret),
                "api_base_url": qq_base_url,
                "allowed_publications": qq_pubs,
                "auto_execute_capabilities": qq_auto,
                "workspace_id": qq_ws,
                "isolate_workspaces": qq_iso,
                "gateway_state": _gateway_state(plane, "qq_gateway", configured=qq_configured),
            },
            "wecom": {
                "configured": wc_configured,
                "bot_id": wc_id,
                "bot_secret_configured": bool(wc_secret),
                "bot_secret_masked": mask_secret(wc_secret),
                "ws_url": wc_ws_url,
                "allowed_publications": wc_pubs,
                "auto_execute_capabilities": wc_auto,
                "workspace_id": wc_ws,
                "isolate_workspaces": wc_iso,
                "group_direct_on_callback": wc_direct,
                "gateway_state": _gateway_state(plane, "wecom_gateway", configured=wc_configured),
            },
        },
    }


def _save_model_config(plane: ControlPlane, config_path: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    existing = _load_env_safe(config_path)
    raw_url = str(body.get("url") or "").strip()
    raw_name = str(body.get("name") or "").strip()
    raw_key = str(body.get("api_key") or "").strip()
    effective_key = raw_key or (existing.get("AGENT_MODEL_API_KEY") or existing.get("DEEPSEEK_API_KEY") or "").strip()
    if not raw_url or not raw_name or not effective_key:
        raise ValueError("主模型 API 地址、模型名称与 API Key 均不能为空")
    url = RuntimeConfig._validate_model_url(raw_url, label="model URL")
    if any(ch in raw_name + effective_key for ch in "\r\n\x00"):
        raise ValueError("model name and API key must be single-line values")

    try:
        ctx_win = int(body.get("context_window") or DEFAULT_MODEL_CONTEXT_WINDOW)
    except (TypeError, ValueError) as error:
        raise ValueError("context_window must be an integer") from error
    if not 4096 <= ctx_win <= 4_194_304:
        raise ValueError("context_window must be between 4096 and 4194304")

    try:
        max_tok = int(body.get("max_tokens") or min(DEFAULT_MODEL_MAX_TOKENS, ctx_win))
    except (TypeError, ValueError) as error:
        raise ValueError("max_tokens must be an integer") from error
    if not 512 <= max_tok <= min(2_097_152, ctx_win):
        raise ValueError(f"max_tokens must be between 512 and {min(2_097_152, ctx_win)}")

    reasoning = str(body.get("reasoning_effort") or "").strip().lower()
    if reasoning and reasoning not in VALID_REASONING_EFFORTS:
        raise ValueError("reasoning_effort must be one of: off, low, medium, high")

    fb_name = str(body.get("fallback_name") or "").strip()
    fb_url_raw = str(body.get("fallback_url") or "").strip()
    fb_key_raw = str(body.get("fallback_api_key") or "").strip()
    clear_fb_key = bool(body.get("clear_fallback_api_key", False))
    if any(ch in fb_name + fb_key_raw for ch in "\r\n\x00"):
        raise ValueError("fallback model name and API key must be single-line values")
    fb_url = RuntimeConfig._validate_model_url(fb_url_raw, label="fallback model URL") if fb_url_raw else ""
    if not fb_name:
        effective_fb_key: str | None = None
        effective_fb_url: str | None = None
    else:
        effective_fb_url = fb_url or None
        if clear_fb_key:
            effective_fb_key = None
        elif fb_key_raw:
            effective_fb_key = fb_key_raw
        else:
            effective_fb_key = (existing.get("AGENT_MODEL_FALLBACK_API_KEY") or "").strip() or None

    updates: dict[str, str | None] = {
        "AGENT_MODEL_URL": url,
        "AGENT_MODEL_NAME": raw_name,
        "AGENT_MODEL_API_KEY": effective_key,
        "AGENT_MODEL_CONTEXT_WINDOW": str(ctx_win),
        "AGENT_MODEL_MAX_TOKENS": str(max_tok),
        "AGENT_MODEL_REASONING_EFFORT": reasoning or None,
        "AGENT_MODEL_FALLBACK_NAME": fb_name or None,
        "AGENT_MODEL_FALLBACK_URL": effective_fb_url,
        "AGENT_MODEL_FALLBACK_API_KEY": effective_fb_key,
    }
    if "DEEPSEEK_BASE_URL" in existing:
        updates["DEEPSEEK_BASE_URL"] = url
    if "DEEPSEEK_API_KEY" in existing:
        updates["DEEPSEEK_API_KEY"] = effective_key

    update_runtime_env_file(config_path, updates)
    RuntimeConfig.load(config_path)
    plane.record_external_audit("model_config_updated", {
        "model_url": url,
        "model_name": raw_name,
        "api_key_changed": bool(raw_key),
        "context_window": ctx_win,
        "max_tokens": max_tok,
        "reasoning_effort": reasoning or None,
        "fallback_name": fb_name or None,
        "fallback_url": effective_fb_url,
        "fallback_api_key_changed": bool(fb_key_raw),
    })
    return _runtime_config_payload(plane, config_path)


def _test_model_connection(config_path: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    existing = _load_env_safe(config_path)
    use_fallback = bool(body.get("test_fallback", False))
    if use_fallback:
        raw_url = str(body.get("fallback_url") or body.get("url") or existing.get("AGENT_MODEL_FALLBACK_URL") or existing.get("AGENT_MODEL_URL") or "").strip()
        raw_name = str(body.get("fallback_name") or existing.get("AGENT_MODEL_FALLBACK_NAME") or "").strip()
        raw_key = (
            str(body.get("fallback_api_key") or "").strip()
            or (existing.get("AGENT_MODEL_FALLBACK_API_KEY") or "").strip()
            or str(body.get("api_key") or "").strip()
            or (existing.get("AGENT_MODEL_API_KEY") or existing.get("DEEPSEEK_API_KEY") or "").strip()
        )
    else:
        raw_url = str(body.get("url") or existing.get("AGENT_MODEL_URL") or existing.get("DEEPSEEK_BASE_URL") or "").strip()
        raw_name = str(body.get("name") or existing.get("AGENT_MODEL_NAME") or "").strip()
        raw_key = str(body.get("api_key") or "").strip() or (existing.get("AGENT_MODEL_API_KEY") or existing.get("DEEPSEEK_API_KEY") or "").strip()

    if not raw_url or not raw_name or not raw_key:
        raise ValueError("测试模型连通性需要填写 API 地址、模型名称与 API Key")
    url = RuntimeConfig._validate_model_url(raw_url, label="model URL")
    if any(ch in raw_name + raw_key for ch in "\r\n\x00"):
        raise ValueError("model name and API key must be single-line values")

    headers = {
        "Authorization": f"Bearer {raw_key}",
        "User-Agent": "company-agent-console/1.0",
        "Accept": "application/json",
    }
    models_url = f"{url}/models"
    try:
        req = Request(models_url, headers=headers, method="GET")
        with urlopen(req, timeout=8.0) as resp:
            raw_data = json.loads(resp.read(262_144).decode("utf-8", errors="replace"))
        items = raw_data.get("data") if isinstance(raw_data, dict) else None
        model_ids = [
            str(item.get("id")) for item in (items or [])
            if isinstance(item, dict) and item.get("id")
        ]
        listed = raw_name in model_ids
        msg = (
            f"模型接口连通成功，已确认模型 {raw_name} 在可用列表中（共 {len(model_ids)} 个模型）。"
            if listed
            else (
                f"网关 /models 连通成功（返回 {len(model_ids)} 个模型），但未在列表中找到 {raw_name}；请确认模型名是否正确。"
                if model_ids
                else f"模型接口 {url} 连通成功。"
            )
        )
        return {
            "ok": True,
            "url": url,
            "model": raw_name,
            "model_listed": listed,
            "available_models": model_ids[:40],
            "message": msg,
        }
    except HTTPError as http_err:
        raise ValueError(f"模型接口返回 HTTP {http_err.code}：请检查 API 地址或密钥是否有效") from http_err
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as net_err:
        raise ValueError(f"无法连接模型接口 {url}：{type(net_err).__name__}") from net_err


def _save_channel_config(plane: ControlPlane, config_path: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    existing = _load_env_safe(config_path)
    channel = str(body.get("channel") or "").strip().lower()
    if channel == "qq":
        app_id = str(body.get("app_id") or "").strip()
        raw_secret = str(body.get("client_secret") or "").strip()
        effective_secret = raw_secret or (existing.get("QQBOT_CLIENT_SECRET") or existing.get("QQBOT_SECRET") or "").strip()
        if not app_id or not effective_secret:
            raise ValueError("QQ 机器人 AppID 与 ClientSecret 均不能为空")
        api_base_url = str(body.get("api_base_url") or "https://api.bot.qq.com").strip().rstrip("/")
        allowed_pubs = str(body.get("allowed_publications") or "group_update").strip()
        auto_exec = str(body.get("auto_execute_capabilities") if "auto_execute_capabilities" in body else "dsh.task").strip()
        workspace_id = str(body.get("workspace_id") or "qq-default").strip()
        isolate_ws = bool(body.get("isolate_workspaces", True))

        updates: dict[str, str | None] = {
            "QQBOT_APP_ID": app_id,
            "QQBOT_CLIENT_SECRET": effective_secret,
            "QQBOT_API_BASE_URL": api_base_url,
            "QQBOT_ALLOWED_PUBLICATIONS": allowed_pubs,
            "QQBOT_AUTO_EXECUTE_CAPABILITIES": auto_exec,
            "COMPANY_AGENT_QQ_WORKSPACE": workspace_id,
            "COMPANY_AGENT_QQ_ISOLATE_WORKSPACES": "true" if isolate_ws else "false",
        }
        if "QQBOT_APPID" in existing:
            updates["QQBOT_APPID"] = app_id
        if "QQBOT_SECRET" in existing:
            updates["QQBOT_SECRET"] = effective_secret

        update_runtime_env_file(config_path, updates)
        QQBotSettings.load(config_path)
        plane.record_external_audit("channel_config_updated", {
            "channel": "qq",
            "app_id": app_id,
            "secret_changed": bool(raw_secret),
            "api_base_url": api_base_url,
            "allowed_publications": allowed_pubs,
            "auto_execute_capabilities": auto_exec,
            "workspace_id": workspace_id,
            "isolate_workspaces": isolate_ws,
        })
        return _runtime_config_payload(plane, config_path)

    if channel == "wecom":
        bot_id = str(body.get("bot_id") or "").strip()
        raw_secret = str(body.get("bot_secret") or "").strip()
        effective_secret = raw_secret or (existing.get("WECOM_BOT_SECRET") or existing.get("COMPANY_AGENT_WECOM_BOT_SECRET") or "").strip()
        if not bot_id or not effective_secret:
            raise ValueError("企业微信 BotID 与 Secret 均不能为空")
        ws_url = str(body.get("ws_url") or WECOM_DEFAULT_WS_URL).strip()
        allowed_pubs = str(body.get("allowed_publications") or "group_update").strip()
        auto_exec = str(body.get("auto_execute_capabilities") if "auto_execute_capabilities" in body else "dsh.task").strip()
        workspace_id = str(body.get("workspace_id") or "wecom-default").strip()
        isolate_ws = bool(body.get("isolate_workspaces", True))
        group_direct = bool(body.get("group_direct_on_callback", True))

        updates_wc: dict[str, str | None] = {
            "WECOM_BOT_ID": bot_id,
            "WECOM_BOT_SECRET": effective_secret,
            "WECOM_WS_URL": ws_url,
            "WECOM_ALLOWED_PUBLICATIONS": allowed_pubs,
            "WECOM_AUTO_EXECUTE_CAPABILITIES": auto_exec,
            "COMPANY_AGENT_WECOM_WORKSPACE": workspace_id,
            "COMPANY_AGENT_WECOM_ISOLATE_WORKSPACES": "true" if isolate_ws else "false",
            "WECOM_GROUP_DIRECT_ON_CALLBACK": "true" if group_direct else "false",
        }
        if "COMPANY_AGENT_WECOM_BOT_ID" in existing:
            updates_wc["COMPANY_AGENT_WECOM_BOT_ID"] = bot_id
        if "COMPANY_AGENT_WECOM_BOT_SECRET" in existing:
            updates_wc["COMPANY_AGENT_WECOM_BOT_SECRET"] = effective_secret

        update_runtime_env_file(config_path, updates_wc)
        WeComBotSettings.load(config_path)
        plane.record_external_audit("channel_config_updated", {
            "channel": "wecom",
            "bot_id": bot_id,
            "secret_changed": bool(raw_secret),
            "ws_url": ws_url,
            "allowed_publications": allowed_pubs,
            "auto_execute_capabilities": auto_exec,
            "workspace_id": workspace_id,
            "isolate_workspaces": isolate_ws,
            "group_direct_on_callback": group_direct,
        })
        return _runtime_config_payload(plane, config_path)

    raise ValueError("channel must be 'qq' or 'wecom'")


def _test_channel_connection(config_path: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    import socket
    import ssl

    existing = _load_env_safe(config_path)
    channel = str(body.get("channel") or "").strip().lower()
    if channel == "qq":
        app_id = str(body.get("app_id") or existing.get("QQBOT_APP_ID") or existing.get("QQBOT_APPID") or "").strip()
        secret = str(body.get("client_secret") or "").strip() or (existing.get("QQBOT_CLIENT_SECRET") or existing.get("QQBOT_SECRET") or "").strip()
        api_base_url = str(body.get("api_base_url") or existing.get("QQBOT_API_BASE_URL") or "https://api.bot.qq.com").strip().rstrip("/")
        if not app_id or not secret:
            raise ValueError("测试 QQ 机器人需要提供 AppID 和 ClientSecret")
        if any(ch in app_id + secret for ch in "\r\n\x00"):
            raise ValueError("QQ bot credentials must be single-line values")
        parsed = urlsplit(api_base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("QQBOT_API_BASE_URL must be an HTTPS base URL without credentials or query")
        settings = QQBotSettings(app_id=app_id, client_secret=secret, api_base_url=api_base_url)
        try:
            token = QQBotAPI(settings, timeout=8.0).access_token()
        except Exception as err:
            raise ValueError(f"QQ 机器人凭据校验失败：{err}") from err
        return {
            "ok": True,
            "channel": "qq",
            "app_id": app_id,
            "token_obtained": bool(token),
            "message": f"QQ 机器人 (AppID: {app_id}) 凭据校验通过，已成功换取 Access Token。",
        }

    if channel == "wecom":
        bot_id = str(body.get("bot_id") or existing.get("WECOM_BOT_ID") or existing.get("COMPANY_AGENT_WECOM_BOT_ID") or "").strip()
        secret = str(body.get("bot_secret") or "").strip() or (existing.get("WECOM_BOT_SECRET") or existing.get("COMPANY_AGENT_WECOM_BOT_SECRET") or "").strip()
        ws_url = str(body.get("ws_url") or existing.get("WECOM_WS_URL") or WECOM_DEFAULT_WS_URL).strip()
        if not bot_id or not secret:
            raise ValueError("测试企业微信机器人需要提供 BotID 和 Secret")
        if any(ch in bot_id + secret for ch in "\r\n\x00"):
            raise ValueError("WeCom bot credentials must be single-line values")
        parsed = urlsplit(ws_url)
        if parsed.scheme != "wss" or not parsed.netloc or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("WECOM_WS_URL must be a wss:// URL without embedded credentials or fragment")
        host = parsed.hostname or ""
        port = parsed.port or 443
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=6.0) as tcp_sock:
                with ctx.wrap_socket(tcp_sock, server_hostname=host):
                    pass
        except Exception as err:
            raise ValueError(f"企业微信网关 {host}:{port} TLS 连通失败：{type(err).__name__}") from err
        return {
            "ok": True,
            "channel": "wecom",
            "bot_id": bot_id,
            "ws_url": ws_url,
            "message": f"企业微信网关 ({host}:{port}) TLS 连通性及凭据格式校验通过（为防踢掉在线长连接，未在探测中重复发送 subscribe 帧）。",
        }

    raise ValueError("channel must be 'qq' or 'wecom'")
