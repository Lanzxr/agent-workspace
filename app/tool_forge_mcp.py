from __future__ import annotations

import hmac
import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from uuid import uuid4

from .control_plane import ControlPlane
from .shared_tools import SharedToolsManager
from .tool_forge import (
    DEFAULT_FORGE_MCP_HOST,
    DEFAULT_FORGE_MCP_PORT,
    ForgeSecretsVault,
    ToolForgeError,
    ToolForgeSecurityError,
    ToolForgeService,
    redact_text,
)

SUPPORTED_MCP_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
ENDPOINT_TIER_MAP = {
    "/mcp/forge": "admin",
    "/mcp/internal": "internal",
    "/mcp/common": "common",
}
_SCHEMA_CONSTRAINT_KEEP = {"type", "oneOf", "properties", "required", "additionalProperties", "items", "enum", "const"}
_SCHEMA_ANNOTATION_KEEP = {"description", "title", "default", "examples"}
_MAX_BODY_BYTES = 262_144


def _json_obj_schema(properties: Mapping[str, Any], required: list[str] | tuple[str, ...] = (), *, additional_properties: bool = False) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": additional_properties,
    }


def _project_mcp_input_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Strip JSON Schema keywords rejected by DSH's strict subset while preserving descriptions."""

    def _clean_node(node: Any) -> Any:
        if not isinstance(node, Mapping):
            return {"description": "JSON value"}
        out: dict[str, Any] = {}
        extra_hints: list[str] = []
        for key in ("minimum", "maximum", "minLength", "maxLength"):
            if key in node:
                extra_hints.append(f"{key}={node[key]}")
        for key, value in node.items():
            if key in _SCHEMA_CONSTRAINT_KEEP or key in _SCHEMA_ANNOTATION_KEEP:
                if key == "properties" and isinstance(value, Mapping):
                    out["properties"] = {str(k): _clean_node(v) for k, v in value.items()}
                elif key == "items" and isinstance(value, Mapping):
                    out["items"] = _clean_node(value)
                elif key == "oneOf" and isinstance(value, list):
                    out["oneOf"] = [_clean_node(v) for v in value]
                else:
                    out[key] = value
        node_type = out.get("type")
        if node_type == "object":
            out.setdefault("properties", {})
            out.setdefault("additionalProperties", True)
        elif node_type == "array":
            out.setdefault("items", {"type": "string"})
        if extra_hints:
            base_desc = str(out.get("description") or "").strip()
            suffix = f"({', '.join(extra_hints)})"
            out["description"] = f"{base_desc} {suffix}".strip() if base_desc else suffix
        return out

    cleaned = _clean_node(schema)
    if cleaned.get("type") != "object":
        return _json_obj_schema({}, [])
    cleaned.setdefault("properties", {})
    cleaned.setdefault("required", [])
    cleaned.setdefault("additionalProperties", False)
    return cleaned


def _format_dynamic_description(tool: Mapping[str, Any]) -> str:
    op_tag = "[写入]" if tool.get("operation_type") in {"write", "mutating"} else "[只读]"
    parts = [f"{op_tag} {tool.get('description') or tool.get('tool_id')}"]
    when_to_use = str(tool.get("when_to_use") or "").strip()
    when_not_to_use = str(tool.get("when_not_to_use") or "").strip()
    if when_to_use:
        parts.append(f"适用场景: {when_to_use}")
    if when_not_to_use:
        parts.append(f"禁止场景: {when_not_to_use}")
    parts.append(f"(v{tool.get('version', 1)}, {tool.get('kind', 'http_api')})")
    return " | ".join(parts)


_FORGE_BUILTIN_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "forge_list_capabilities",
        "description": "[只读] 查看 Tool Forge 当前可用的域名白名单、密钥别名清单（不含明文值）、以及已登记的动态工具和待审批提案。",
        "inputSchema": _json_obj_schema({}, []),
    },
    {
        "name": "forge_probe_http",
        "description": "[只读] 在网络允许白名单内试调外部 HTTP/HTTPS 接口（支持 {{SECRET_NAME}} 密钥占位符自动注入，返回已脱敏响应，供编写复用工具前确认字段格式）。",
        "inputSchema": _json_obj_schema(
            {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "HTTP 请求方法"},
                "url": {"type": "string", "description": "目标完整 URL（https:// 或已允许 http://），主机必须在管理员白名单中"},
                "headers": {"type": "object", "description": "可选请求头对象，值中可用 {{SECRET_ALIAS}}", "additionalProperties": True},
                "params": {"type": "object", "description": "可选 URL 查询参数对象", "additionalProperties": True},
                "json_body": {"description": "可选 JSON 请求体（任意 JSON 值）"},
                "timeout_seconds": {"type": "number", "description": "超时秒数（1-30，默认 15）"},
            },
            ["url"],
        ),
    },
    {
        "name": "forge_draft_tool",
        "description": "[写入] 起草或升级一个可复用工具版本（http_api 声明式接口 / python_plugin 受限聚合脚本 / shared_script 离线工作区脚本）。草稿不会立即上线。",
        "inputSchema": _json_obj_schema(
            {
                "tool_id": {"type": "string", "description": "工具唯一标识（小写字母+数字+下划线，3-48位，不能以 forge_ 开头）"},
                "kind": {"type": "string", "enum": ["http_api", "python_plugin", "shared_script"], "description": "工具类型"},
                "description": {"type": "string", "description": "清晰的功能描述（10-500字符），决定全员模型能否正确选型"},
                "when_to_use": {"type": "string", "description": "何时应优先调用该工具"},
                "when_not_to_use": {"type": "string", "description": "何时不应调用（如误用场景/缺权限场景）"},
                "operation_type": {"type": "string", "enum": ["read", "write"], "description": "读操作还是写操作"},
                "target_visibility": {"type": "string", "enum": ["admin", "internal", "common"], "description": "拟发布范围：admin(仅管理员)/internal(员工群+管理员)/common(全部含客户群)"},
                "input_schema": {"type": "object", "description": "标准 JSON Schema object，需声明 properties/required 及每个参数的 description", "additionalProperties": True},
                "http_spec": {"type": "object", "description": "kind=http_api 必填：{method, url_template, headers, query_template, json_body_template, pagination, result_path}", "additionalProperties": True},
                "python_code": {"type": "string", "description": "kind=python_plugin 必填：需定义 def run(args, ctx): ...，可用 ctx.http.get/post 和 ctx.hmac_sha256"},
                "script_name": {"type": "string", "description": "kind=shared_script 必填：脚本文件名（如 calc_margin.py）"},
                "script_content": {"type": "string", "description": "kind=shared_script 必填：完整离线脚本源码"},
                "allowed_domains": {"type": "array", "items": {"type": "string"}, "description": "该工具依赖的外部域名列表"},
                "default_test_input": {"type": "object", "description": "用于 forge_test_tool 回归测试的默认输入参数", "additionalProperties": True},
            },
            ["tool_id", "kind", "description"],
        ),
    },
    {
        "name": "forge_test_tool",
        "description": "[只读] 使用隔离测试执行器验证已起草的 Tool Forge 工具版本，并记录带代码哈希的测试报告。",
        "inputSchema": _json_obj_schema(
            {
                "tool_id": {"type": "string", "description": "待测试工具 ID"},
                "version": {"type": "integer", "description": "可选指定版本号，默认取最新版本"},
                "arguments": {"type": "object", "description": "可选测试入参，不传则使用 default_test_input", "additionalProperties": True},
            },
            ["tool_id"],
        ),
    },
    {
        "name": "forge_submit_release",
        "description": "[写入] 将已通过测试的工具版本提交上线审批提案，生成 proposal_id；需由管理员本人在 QQ 私聊发送 /forge-approve <proposal_id> 或在控制台批准后才对目标层级生效。",
        "inputSchema": _json_obj_schema(
            {
                "tool_id": {"type": "string", "description": "待提交发布的工具 ID"},
                "version": {"type": "integer", "description": "可选指定版本号"},
                "target_visibility": {"type": "string", "enum": ["admin", "internal", "common"], "description": "发布作用域"},
                "summary": {"type": "string", "description": "给管理员看的简要变更与影响说明"},
            },
            ["tool_id"],
        ),
    },
    {
        "name": "forge_list_tools",
        "description": "[只读] 列出 Tool Forge 已登记的全部工具、当前启用版本、调用统计及待审批提案。",
        "inputSchema": _json_obj_schema({}, []),
    },
    {
        "name": "forge_get_tool",
        "description": "[只读] 查看指定动态工具的完整定义、测试报告及历史版本详情。",
        "inputSchema": _json_obj_schema(
            {
                "tool_id": {"type": "string", "description": "工具 ID"},
                "version": {"type": "integer", "description": "可选版本号，默认返回活跃版本或最新版本"},
            },
            ["tool_id"],
        ),
    },
    {
        "name": "forge_toggle_tool",
        "description": "[写入] 启用或停用已注册的动态工具，立即影响各层级 MCP 暴露。",
        "inputSchema": _json_obj_schema(
            {
                "tool_id": {"type": "string", "description": "工具 ID"},
                "enabled": {"type": "boolean", "description": "true 启用，false 停用"},
            },
            ["tool_id", "enabled"],
        ),
    },
    {
        "name": "forge_rollback_tool",
        "description": "[写入] 将指定工具回滚到上一个或指定已批准的历史版本并立即重新上线。",
        "inputSchema": _json_obj_schema(
            {
                "tool_id": {"type": "string", "description": "工具 ID"},
                "target_version": {"type": "integer", "description": "可选目标已审批版本号；不传则回滚到紧邻上一已审批版"},
            },
            ["tool_id"],
        ),
    },
)


def _dispatch_builtin_forge_tool(service: ToolForgeService, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    if name == "forge_list_capabilities":
        return {
            "domains": service.list_domains(),
            "secrets": service.list_secrets_metadata(),
            "tools": service.list_tools(),
            "pending_proposals": service.list_proposals(status="pending", limit=50),
        }
    if name == "forge_probe_http":
        return service.probe_http(
            method=str(args.get("method") or "GET"),
            url=str(args.get("url") or ""),
            headers=args.get("headers") if isinstance(args.get("headers"), Mapping) else None,
            params=args.get("params") if isinstance(args.get("params"), Mapping) else None,
            json_body=args.get("json_body"),
            timeout_seconds=float(args.get("timeout_seconds") or 15.0),
        )
    if name == "forge_draft_tool":
        return service.draft_tool(
            tool_id=str(args.get("tool_id") or ""),
            kind=str(args.get("kind") or ""),
            description=str(args.get("description") or ""),
            when_to_use=str(args.get("when_to_use") or ""),
            when_not_to_use=str(args.get("when_not_to_use") or ""),
            operation_type=str(args.get("operation_type") or "read"),
            target_visibility=str(args.get("target_visibility") or "internal"),
            input_schema=args.get("input_schema") if isinstance(args.get("input_schema"), Mapping) else None,
            http_spec=args.get("http_spec") if isinstance(args.get("http_spec"), Mapping) else None,
            python_code=str(args.get("python_code") or ""),
            script_name=str(args.get("script_name") or ""),
            script_content=str(args.get("script_content") or ""),
            allowed_domains=list(args.get("allowed_domains") or []) if isinstance(args.get("allowed_domains"), list) else None,
            default_test_input=args.get("default_test_input") if isinstance(args.get("default_test_input"), Mapping) else None,
            created_by="admin_agent",
        )
    if name == "forge_test_tool":
        version = args.get("version")
        return service.test_tool(
            str(args.get("tool_id") or ""),
            version=int(version) if version is not None else None,
            arguments=args.get("arguments") if isinstance(args.get("arguments"), Mapping) else None,
        )
    if name == "forge_submit_release":
        version = args.get("version")
        result = service.submit_release(
            str(args.get("tool_id") or ""),
            version=int(version) if version is not None else None,
            target_visibility=str(args["target_visibility"]) if args.get("target_visibility") else None,
            requested_by="admin_agent",
            summary=str(args.get("summary") or ""),
        )
        result["approval_instruction"] = (
            f"请通知管理员本人在 QQ 管理员私聊发送 `/forge-approve {result['proposal_id']}` "
            "或在管理控制台「工具发布审批队列」点击批准上线。"
        )
        return result
    if name == "forge_list_tools":
        return {
            "tools": service.list_tools(),
            "pending_proposals": service.list_proposals(status="pending", limit=50),
        }
    if name == "forge_get_tool":
        version = args.get("version")
        return service.get_tool(
            str(args.get("tool_id") or ""),
            version=int(version) if version is not None else None,
        )
    if name == "forge_toggle_tool":
        return service.set_tool_enabled(
            str(args.get("tool_id") or ""),
            enabled=bool(args.get("enabled")),
            actor="admin_agent",
        )
    if name == "forge_rollback_tool":
        version = args.get("target_version")
        return service.rollback_tool(
            str(args.get("tool_id") or ""),
            target_version=int(version) if version is not None else None,
            actor="admin_agent",
        )
    raise ToolForgeError(f"unknown builtin forge tool: {name}")


def _list_mcp_tools_for_endpoint(service: ToolForgeService, endpoint_tier: str) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    if endpoint_tier == "admin":
        tools.extend(dict(spec) for spec in _FORGE_BUILTIN_TOOLS)
    for item in service.list_active_tools_for_tier(endpoint_tier):
        tools.append(
            {
                "name": item["tool_id"],
                "description": _format_dynamic_description(item),
                "inputSchema": _project_mcp_input_schema(item.get("input_schema") or {}),
            }
        )
    return tools


def _call_mcp_tool_for_endpoint(
    service: ToolForgeService,
    endpoint_tier: str,
    name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    builtin_names = {spec["name"] for spec in _FORGE_BUILTIN_TOOLS}
    if name in builtin_names:
        if endpoint_tier != "admin":
            raise ToolForgeSecurityError(f"tool {name!r} is restricted to admin tier")
        return _dispatch_builtin_forge_tool(service, name, arguments)
    return {"tool_id": name, "result": service.call_active_tool(name, arguments, caller_tier=endpoint_tier)}


def make_tool_forge_mcp_handler(
    service_factory: Callable[[], ToolForgeService],
    tokens: Mapping[str, str],
):
    token_by_tier = {tier: str(tokens.get(tier) or "") for tier in ("admin", "internal", "common")}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(self, status: int, payload: Any, *, extra_headers: Mapping[str, str] | None = None) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def _send_empty(self, status: int, *, extra_headers: Mapping[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()

        def _endpoint_tier(self) -> str | None:
            path = self.path.split("?", 1)[0]
            return ENDPOINT_TIER_MAP.get(path)

        def _authorized_tier(self, tier: str) -> bool:
            expected = token_by_tier.get(tier, "")
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not expected or not header.startswith(prefix):
                return False
            return hmac.compare_digest(header[len(prefix):].strip(), expected)

        def _handle_rpc_message(self, service: ToolForgeService, tier: str, msg: Any) -> tuple[dict[str, Any] | None, str | None]:
            if not isinstance(msg, Mapping):
                return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}, None
            method = msg.get("method")
            req_id = msg.get("id")
            if not isinstance(method, str):
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32600, "message": "Invalid Request"}}, None
            # Notifications have no id (or id is omitted); MCP expects 202 Accepted with empty body.
            if "id" not in msg:
                return None, None

            params = msg.get("params") if isinstance(msg.get("params"), Mapping) else {}
            if method == "initialize":
                requested_proto = str(params.get("protocolVersion") or "")
                proto = requested_proto if requested_proto in SUPPORTED_MCP_PROTOCOLS else "2025-03-26"
                session_id = f"forge-{tier}-{uuid4().hex[:12]}"
                return (
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": proto,
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": f"company-agent-forge-{tier}", "version": "1.0.0"},
                        },
                    },
                    session_id,
                )
            if method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}, None
            if method == "tools/list":
                tools = _list_mcp_tools_for_endpoint(service, tier)
                return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}, None
            if method == "tools/call":
                tool_name = str(params.get("name") or "").strip()
                raw_args = params.get("arguments")
                args_map = raw_args if isinstance(raw_args, Mapping) else {}
                try:
                    payload = _call_mcp_tool_for_endpoint(service, tier, tool_name, args_map)
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                            "isError": False,
                        },
                    }, None
                except Exception as exc:
                    err_msg = redact_text(str(exc), service.secrets_vault.all_secret_values())
                    return {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": err_msg[:2000]}],
                            "isError": True,
                        },
                    }, None
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }, None

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                service = service_factory()
                active_count = len(service.list_active_tools_for_tier("admin"))
                self._send_json(200, {"ok": True, "service": "tool-forge-mcp", "active_tools": active_count})
                return
            tier = self._endpoint_tier()
            if tier is None:
                self._send_json(404, {"error": "not found"})
                return
            if not self._authorized_tier(tier):
                self._send_json(401, {"error": "unauthorized"})
                return
            # Stateless Streamable HTTP: GET SSE stream is intentionally declined with 405.
            self._send_empty(HTTPStatus.METHOD_NOT_ALLOWED, extra_headers={"Allow": "POST, DELETE"})

        def do_DELETE(self) -> None:
            tier = self._endpoint_tier()
            if tier is None:
                self._send_json(404, {"error": "not found"})
                return
            if not self._authorized_tier(tier):
                self._send_json(401, {"error": "unauthorized"})
                return
            self._send_json(200, {"ok": True})

        def do_POST(self) -> None:
            tier = self._endpoint_tier()
            if tier is None:
                self._send_json(404, {"error": "not found"})
                return
            if not self._authorized_tier(tier):
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= _MAX_BODY_BYTES:
                    self._send_json(413, {"error": "request body exceeds 256 KiB"})
                    return
                body_raw = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(body_raw)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)[:200]}})
                return

            service = service_factory()
            if isinstance(payload, list):
                if not payload:
                    self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Empty batch"}})
                    return
                responses: list[dict[str, Any]] = []
                session_id = None
                for item in payload:
                    resp, sid = self._handle_rpc_message(service, tier, item)
                    if sid and session_id is None:
                        session_id = sid
                    if resp is not None:
                        responses.append(resp)
                if not responses:
                    self._send_empty(HTTPStatus.ACCEPTED)
                    return
                headers = {"Mcp-Session-Id": session_id} if session_id else None
                self._send_json(200, responses, extra_headers=headers)
                return

            resp, session_id = self._handle_rpc_message(service, tier, payload)
            if resp is None:
                self._send_empty(HTTPStatus.ACCEPTED)
                return
            headers = {"Mcp-Session-Id": session_id} if session_id else None
            self._send_json(200, resp, extra_headers=headers)

    return Handler


class ToolForgeMcpServer:
    """Loopback Streamable HTTP MCP server backed directly by ControlPlane/ToolForgeService."""

    def __init__(
        self,
        *,
        plane: ControlPlane,
        secrets_vault: ForgeSecretsVault,
        shared_tools: SharedToolsManager | None,
        tokens: Mapping[str, str],
        host: str = DEFAULT_FORGE_MCP_HOST,
        port: int = DEFAULT_FORGE_MCP_PORT,
    ) -> None:
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("tool-forge-mcp can only bind to loopback host")
        self.service = ToolForgeService(plane, secrets_vault=secrets_vault, shared_tools=shared_tools)
        self.tokens = dict(tokens)
        self.server = ThreadingHTTPServer(
            (host, int(port)),
            make_tool_forge_mcp_handler(lambda: self.service, self.tokens),
        )
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="company-agent-tool-forge-mcp", daemon=True)

    @property
    def address(self) -> tuple[str, int]:
        addr = self.server.server_address
        return str(addr[0]), int(addr[1])

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=3)


__all__ = [
    "ENDPOINT_TIER_MAP",
    "ToolForgeMcpServer",
    "_project_mcp_input_schema",
    "make_tool_forge_mcp_handler",
]
