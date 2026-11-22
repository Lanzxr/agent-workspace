from __future__ import annotations

import json
import os
import tempfile
import unittest
from http.server import HTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.admin_api import make_handler
from app.assistant_workflow import DssuperAgentWorkflow
from app.control_plane import ControlPlane
from app.dsh import DshAdapter
from app.identity import IdentityRegistry
from app.models import Event, ExecutionRequest
from app.qq_channel import QQBotAPI, QQChannelService
from app.runtime_config import QQBotSettings, RuntimeConfig, parse_forge_mcp_settings
from app.shared_tools import SharedToolsManager
from app.tool_forge import (
    ForgeSecretsVault,
    ToolForgeSecurityError,
    ToolForgeService,
    ToolForgeValidationError,
    build_forge_mcp_specs,
    ensure_forge_mcp_tokens,
    forge_mcp_secrets_map,
    validate_plugin_ast,
)
from app.tool_forge_mcp import ToolForgeMcpServer, _project_mcp_input_schema
from app.workspace import WorkspaceManager


def _http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: object = None):
    raw = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = dict(headers or {})
    if raw is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = Request(url, data=raw, headers=req_headers, method=method)
    with urlopen(req, timeout=5) as resp:
        text = resp.read().decode("utf-8")
        return resp.status, (json.loads(text) if text.strip() else None), dict(resp.headers)


class _DummyQQApi(QQBotAPI):
    def __init__(self) -> None:
        super().__init__(QQBotSettings("app-1", "secret-1", allowed_publications=frozenset({"group_update", "suppress"})))
        self.sent: list[tuple[str, str]] = []

    def send_group_message(self, message) -> None:
        self.sent.append(("group", message.body))

    def send_c2c_message(self, message) -> None:
        self.sent.append(("c2c", message.body))


class ToolForgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plane = ControlPlane.open(self.root / "control.sqlite")
        self.shared = SharedToolsManager(self.root / "shared-tools")
        self.shared.ensure_layers()
        self.vault = ForgeSecretsVault(self.root / "forge-secrets.json")

        def fake_dns(host: str, port: int) -> list[str]:
            if host == "rebind.evil.test":
                return ["127.0.0.1"]
            if host == "lan-leak.evil.test":
                return ["10.0.0.5"]
            return ["93.184.216.34"]

        self.http_calls: list[dict[str, object]] = []

        def fake_opener(req: Request, timeout: float):
            hdrs = {k.lower(): v for k, v in req.header_items()}
            body_bytes = req.data or b""
            self.http_calls.append({
                "method": req.get_method(),
                "url": req.full_url,
                "headers": hdrs,
                "body": body_bytes.decode("utf-8", errors="replace"),
            })
            if "model-logs" in req.full_url:
                if "page=2" in req.full_url:
                    payload = {"items": [{"model": "deepseek-chat", "prompt_tokens": 300, "cached_tokens": 50}]}
                else:
                    payload = {
                        "items": [
                            {"model": "deepseek-chat", "prompt_tokens": 1000, "cached_tokens": 200},
                            {"model": "deepseek-reasoner", "prompt_tokens": 500, "cached_tokens": 0},
                        ]
                    }
            else:
                payload = {
                    "echo_auth": hdrs.get("authorization", ""),
                    "query": req.full_url,
                }
            return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode("utf-8")

        self.forge = ToolForgeService(
            self.plane,
            secrets_vault=self.vault,
            shared_tools=self.shared,
            dns_resolver=fake_dns,
            http_opener=fake_opener,
        )

    def tearDown(self) -> None:
        self.plane.close()
        self.tmp.cleanup()

    def test_ssrf_allowlist_and_dns_rebinding_blocked(self) -> None:
        self.forge.add_domain("api.example.com", created_by="admin-1")
        self.forge.add_domain("rebind.evil.test", created_by="admin-1")
        self.forge.add_domain("lan-leak.evil.test", created_by="admin-1")

        # Unlisted domain blocked
        with self.assertRaises(ToolForgeSecurityError):
            self.forge.probe_http(url="https://unlisted.example/v1/users")
        # Localhost & cloud metadata & loopback literal blocked
        with self.assertRaises(ToolForgeSecurityError):
            self.forge.probe_http(url="http://127.0.0.1:8765/api/status")
        with self.assertRaises(ToolForgeSecurityError):
            self.forge.probe_http(url="http://169.254.169.254/latest/meta-data")
        # DNS rebinding to loopback or private IP blocked
        with self.assertRaises(ToolForgeSecurityError):
            self.forge.probe_http(url="https://rebind.evil.test/steal")
        with self.assertRaises(ToolForgeSecurityError):
            self.forge.probe_http(url="https://lan-leak.evil.test/steal")

    def test_secret_vault_redaction_and_http_api_lifecycle(self) -> None:
        secret_val = "sk-super-secret-service-987654"
        self.forge.add_domain("api.example.com", created_by="admin-1")
        meta = self.forge.set_secret("SERVICE_API_KEY", secret_val, actor="admin-1")
        self.assertEqual(meta["name"], "SERVICE_API_KEY")
        self.assertNotIn(secret_val, json.dumps(self.forge.list_secrets_metadata()))
        if os.name != "nt":
            self.assertEqual((self.vault.path.stat().st_mode & 0o777), 0o600)

        # Probe injects secret into header cleanly and redacts it from response preview
        probe_res = self.forge.probe_http(
            url="https://api.example.com/v1/echo",
            headers={"Authorization": "Bearer {{SERVICE_API_KEY}}"},
        )
        self.assertEqual(probe_res["status_code"], 200)
        self.assertEqual(self.http_calls[-1]["headers"]["authorization"], f"Bearer {secret_val}")
        self.assertNotIn(secret_val, json.dumps(probe_res))

        # Draft http_api v1, test, submit, require admin to approve, then hot-call at internal tier
        self.forge.draft_tool(
            tool_id="service_echo",
            kind="http_api",
            description="查询业务示例接口状态并返回基础用户信息",
            when_to_use="管理员或员工查询外部接口连通性时",
            when_not_to_use="无需外部联网的本地计算时",
            operation_type="read",
            target_visibility="internal",
            input_schema={
                "type": "object",
                "properties": {"org": {"type": "string", "description": "组织标识"}},
                "required": ["org"],
            },
            http_spec={
                "method": "GET",
                "url_template": "https://api.example.com/v1/echo",
                "headers": {"Authorization": "Bearer {{SERVICE_API_KEY}}"},
                "query_template": {"org": "{{args.org}}"},
            },
            allowed_domains=["api.example.com"],
            default_test_input={"org": "acme"},
        )

        # Cannot submit before passing test
        with self.assertRaises(ToolForgeValidationError):
            self.forge.submit_release("service_echo")

        test_rep = self.forge.test_tool("service_echo")
        self.assertTrue(test_rep["ok"])
        self.assertNotIn(secret_val, json.dumps(test_rep))

        prop = self.forge.submit_release("service_echo", summary="上线业务查询 v1")
        # Non-admin CANNOT approve
        with self.assertRaises(ToolForgeSecurityError):
            self.forge.approve_proposal(prop["proposal_id"], decided_by="emp-1", is_admin=False)

        approved = self.forge.approve_proposal(prop["proposal_id"], decided_by="admin-1", is_admin=True)
        self.assertEqual(approved["active_version"], 1)

        # Tier isolation: internal can call it, common CANNOT
        out = self.forge.call_active_tool("service_echo", {"org": "acme"}, caller_tier="internal")
        self.assertIn("org=acme", out["json"]["query"])
        self.assertNotIn(secret_val, json.dumps(out))
        with self.assertRaises(ToolForgeSecurityError):
            self.forge.call_active_tool("service_echo", {"org": "acme"}, caller_tier="common")

        # Upgrade draft v2 does NOT break live v1 until v2 is tested & approved, and rollback works cleanly
        self.forge.draft_tool(
            tool_id="service_echo",
            kind="http_api",
            description="查询业务示例接口 v2（增加环境标记）",
            operation_type="read",
            target_visibility="internal",
            input_schema={
                "type": "object",
                "properties": {"org": {"type": "string", "description": "组织标识"}},
                "required": ["org"],
            },
            http_spec={
                "method": "GET",
                "url_template": "https://api.example.com/v2/echo",
                "headers": {"Authorization": "Bearer {{SERVICE_API_KEY}}"},
                "query_template": {"org": "{{args.org}}"},
            },
            allowed_domains=["api.example.com"],
            default_test_input={"org": "beta"},
        )
        # Active is still v1
        still_v1 = self.forge.call_active_tool("service_echo", {"org": "acme"}, caller_tier="internal")
        self.assertIn("/v1/echo", still_v1["json"]["query"])

        self.forge.test_tool("service_echo", version=2)
        prop2 = self.forge.submit_release("service_echo", version=2)
        self.forge.approve_proposal(prop2["proposal_id"], decided_by="admin-1", is_admin=True)
        now_v2 = self.forge.call_active_tool("service_echo", {"org": "acme"}, caller_tier="internal")
        self.assertIn("/v2/echo", now_v2["json"]["query"])

        # Rollback back to v1
        rb = self.forge.rollback_tool("service_echo", actor="admin-1")
        self.assertEqual(rb["active_version"], 1)
        back_v1 = self.forge.call_active_tool("service_echo", {"org": "acme"}, caller_tier="internal")
        self.assertIn("/v1/echo", back_v1["json"]["query"])

        # Disable blocks execution immediately
        self.forge.set_tool_enabled("service_echo", enabled=False, actor="admin-1")
        with self.assertRaises(ToolForgeValidationError):
            self.forge.call_active_tool("service_echo", {"org": "acme"}, caller_tier="internal")

        # Call logs & summary
        tools = {t["tool_id"]: t for t in self.forge.list_tools()}
        self.assertGreaterEqual(tools["service_echo"]["stats"]["calls"], 3)

    def test_python_plugin_ast_security_and_isolated_subprocess_execution(self) -> None:
        # Dangerous imports, dunders, and eval/exec must be rejected statically
        for bad_code in [
            "import os\ndef run(args, ctx):\n    return os.environ\n",
            "import subprocess\ndef run(args, ctx):\n    return {}\n",
            "def run(args, ctx):\n    return ().__class__.__mro__\n",
            "def run(args, ctx):\n    return eval('1+1')\n",
        ]:
            with self.assertRaises(ToolForgeSecurityError):
                validate_plugin_ast(bad_code)

        secret_val = "sk-plugin-only-host-secret"
        self.forge.add_domain("api.example.com", created_by="admin-1")
        self.forge.set_secret("SERVICE_API_KEY", secret_val, actor="admin-1")

        # Plugin aggregates multi-page logs via ctx.http + checks secret wasn't leaked into child env + signs with hmac
        plugin_code = """
import json

def run(args, ctx):
    days = int(args.get("days", 7))
    totals = {}
    for page in (1, 2):
        resp = ctx.http.get(
            "https://api.example.com/model-logs",
            params={"page": page, "days": days},
            headers={"Authorization": "Bearer {{SERVICE_API_KEY}}"},
        )
        for row in resp["json"]["items"]:
            m = row["model"]
            entry = totals.setdefault(m, {"prompt_tokens": 0, "cached_tokens": 0})
            entry["prompt_tokens"] += row["prompt_tokens"]
            entry["cached_tokens"] += row["cached_tokens"]
    sig = ctx.hmac_sha256("SERVICE_API_KEY", f"days={days}")
    return {"totals": totals, "sig_len": len(sig)}
"""
        self.forge.draft_tool(
            tool_id="usage_by_model",
            kind="python_plugin",
            description="按模型聚合提示词与缓存 token 消耗（带分页合并）",
            when_to_use="管理员或内部群需要按模型统计 Token 用量时",
            when_not_to_use="仅需查看单条明细流水时",
            operation_type="read",
            target_visibility="internal",
            input_schema={
                "type": "object",
                "properties": {"days": {"type": "integer", "description": "统计天数", "minimum": 1, "maximum": 90}},
                "required": ["days"],
            },
            python_code=plugin_code,
            allowed_domains=["api.example.com"],
            default_test_input={"days": 7},
        )
        rep = self.forge.test_tool("usage_by_model")
        self.assertTrue(rep["ok"], rep)
        totals = rep["sample_output"]["totals"]
        self.assertEqual(totals["deepseek-chat"]["prompt_tokens"], 1300)
        self.assertEqual(totals["deepseek-chat"]["cached_tokens"], 250)
        self.assertEqual(totals["deepseek-reasoner"]["prompt_tokens"], 500)
        self.assertEqual(rep["sample_output"]["sig_len"], 64)
        self.assertNotIn(secret_val, json.dumps(rep))

        # Project input schema strips keywords rejected by DSH schema subset (e.g. minimum/maximum)
        projected = _project_mcp_input_schema(self.forge.get_tool("usage_by_model")["definition"]["input_schema"])
        self.assertNotIn("minimum", projected["properties"]["days"])
        self.assertIn("minimum=1", projected["properties"]["days"]["description"])

    def test_shared_script_approval_publishes_to_shared_tools(self) -> None:
        self.forge.draft_tool(
            tool_id="margin_helper",
            kind="shared_script",
            description="离线计算商品毛利率并提供公共复用脚本",
            operation_type="read",
            target_visibility="internal",
            script_name="calc_margin_forge.py",
            script_content="#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps({'margin': 0.25}))\n",
        )
        rep = self.forge.test_tool("margin_helper")
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["sample_output"]["validated"])

        prop = self.forge.submit_release("margin_helper")
        self.forge.approve_proposal(prop["proposal_id"], decided_by="admin-1", is_admin=True)
        scripts = {s["name"]: s for s in self.shared.summary()["layers"]["internal"]["scripts"]}
        self.assertIn("calc_margin_forge.py", scripts)

    def test_mcp_server_endpoints_tier_isolation_and_hot_publish(self) -> None:
        self.forge.add_domain("api.example.com", created_by="admin-1")
        tokens = ensure_forge_mcp_tokens(self.root)
        # Re-calling preserves existing tokens
        self.assertEqual(ensure_forge_mcp_tokens(self.root), tokens)
        specs = build_forge_mcp_specs("127.0.0.1", 8768)
        self.assertEqual([s.server_id for s in specs], ["forge_admin", "forge_internal", "forge_common"])
        secrets_map = forge_mcp_secrets_map(tokens)

        # DshAdapter injects active tier forge token AND includes all forge tokens in --unsetenv
        cfg = RuntimeConfig(
            model_url="https://api.example.com/v1",
            model_name="deepseek-chat",
            api_key="model-secret",
            dsh_command=("echo",),
        )
        mgr = WorkspaceManager(self.root / "workspaces")
        ws = mgr.provision("ws-internal-mcp", tier="internal")
        bwrap = self.root / "bwrap"
        bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(bwrap, 0o755)
        adapter = DshAdapter(
            cfg, mgr, self.root / "runtime",
            bubblewrap_executable=bwrap,
            mcp_servers_provider=lambda: specs,
            mcp_secrets_provider=lambda: secrets_map,
        )
        env: dict[str, str] = {}
        adapter._apply_runtime_environment(env, web_enabled=False, workspace_tier="internal")
        self.assertEqual(env["COMPANY_AGENT_MCP_SECRET_FORGE_INTERNAL"], tokens["internal"])
        self.assertNotIn("COMPANY_AGENT_MCP_SECRET_FORGE_ADMIN", env)
        runtime_home = self.root / "runtime"
        runtime_home.mkdir(parents=True, exist_ok=True)
        runner = adapter._write_sandbox_runner(runtime_home, ws.root, workspace_tier="internal")
        runner_text = runner.read_text(encoding="utf-8")
        self.assertIn("--unsetenv COMPANY_AGENT_MCP_SECRET_FORGE_ADMIN", runner_text)
        self.assertIn("--unsetenv COMPANY_AGENT_MCP_SECRET_FORGE_INTERNAL", runner_text)

        mcp_srv = ToolForgeMcpServer(
            plane=self.plane,
            secrets_vault=self.vault,
            shared_tools=self.shared,
            tokens=tokens,
            host="127.0.0.1",
            port=0,
        )
        mcp_srv.start()
        host, port = mcp_srv.address
        base = f"http://{host}:{port}"
        try:
            st, health, _ = _http_json(f"{base}/health")
            self.assertEqual(st, 200)
            self.assertTrue(health["ok"])

            # Cross-tier bearer token rejected
            with self.assertRaises(HTTPError) as ctx:
                _http_json(
                    f"{base}/mcp/forge",
                    method="POST",
                    headers={"Authorization": f"Bearer {tokens['internal']}"},
                    body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()

            # GET /mcp/forge returns 405 Method Not Allowed (stateless Streamable HTTP)
            with self.assertRaises(HTTPError) as ctx:
                _http_json(f"{base}/mcp/forge", method="GET", headers={"Authorization": f"Bearer {tokens['admin']}"})
            self.assertEqual(ctx.exception.code, 405)
            ctx.exception.close()

            admin_hdr = {"Authorization": f"Bearer {tokens['admin']}"}
            int_hdr = {"Authorization": f"Bearer {tokens['internal']}"}

            # Initialize + notifications/initialized -> 202
            _, init_res, resp_hdrs = _http_json(
                f"{base}/mcp/forge",
                method="POST",
                headers=admin_hdr,
                body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            )
            self.assertEqual(init_res["result"]["protocolVersion"], "2025-03-26")
            self.assertIn("mcp-session-id", {k.lower(): v for k, v in resp_hdrs.items()})
            st_notif, _, _ = _http_json(
                f"{base}/mcp/forge",
                method="POST",
                headers=admin_hdr,
                body={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            self.assertEqual(st_notif, 202)

            # Internal initially sees 0 custom tools and 0 forge_* tools
            _, int_tools_before, _ = _http_json(
                f"{base}/mcp/internal",
                method="POST",
                headers=int_hdr,
                body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            self.assertEqual(int_tools_before["result"]["tools"], [])

            # Admin drafts -> tests -> submits proposal via MCP tools!
            _, draft_call, _ = _http_json(
                f"{base}/mcp/forge",
                method="POST",
                headers=admin_hdr,
                body={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "forge_draft_tool",
                        "arguments": {
                            "tool_id": "mcp_service_query",
                            "kind": "http_api",
                            "description": "MCP 热锻造测试工具：查业务样本接口",
                            "target_visibility": "internal",
                            "operation_type": "read",
                            "input_schema": {
                                "type": "object",
                                "properties": {"tag": {"type": "string", "description": "查询标签"}},
                                "required": ["tag"],
                            },
                            "http_spec": {
                                "method": "GET",
                                "url_template": "https://api.example.com/v1/echo",
                                "query_template": {"tag": "{{args.tag}}"},
                            },
                            "allowed_domains": ["api.example.com"],
                            "default_test_input": {"tag": "smoke"},
                        },
                    },
                },
            )
            self.assertFalse(draft_call["result"]["isError"])

            # Swap opener on mcp_srv.service so MCP server uses our deterministic mock
            mcp_srv.service.dns_resolver = self.forge.dns_resolver
            mcp_srv.service.http_opener = self.forge.http_opener

            _, test_call, _ = _http_json(
                f"{base}/mcp/forge",
                method="POST",
                headers=admin_hdr,
                body={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "forge_test_tool", "arguments": {"tool_id": "mcp_service_query"}}},
            )
            self.assertFalse(test_call["result"]["isError"], test_call)

            _, submit_call, _ = _http_json(
                f"{base}/mcp/forge",
                method="POST",
                headers=admin_hdr,
                body={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "forge_submit_release", "arguments": {"tool_id": "mcp_service_query"}}},
            )
            prop_id = json.loads(submit_call["result"]["content"][0]["text"])["proposal_id"]

            # Approve via Admin plane -> IMMEDIATELY hot-appears in /mcp/internal tools/list without server restart!
            mcp_srv.service.approve_proposal(prop_id, decided_by="qq:admin", is_admin=True)
            _, int_tools_after, _ = _http_json(
                f"{base}/mcp/internal",
                method="POST",
                headers=int_hdr,
                body={"jsonrpc": "2.0", "id": 6, "method": "tools/list"},
            )
            self.assertEqual([t["name"] for t in int_tools_after["result"]["tools"]], ["mcp_service_query"])

            _, call_dyn, _ = _http_json(
                f"{base}/mcp/internal",
                method="POST",
                headers=int_hdr,
                body={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "mcp_service_query", "arguments": {"tag": "emp-req"}},
                },
            )
            self.assertFalse(call_dyn["result"]["isError"])
            self.assertIn("tag=emp-req", call_dyn["result"]["content"][0]["text"])
        finally:
            mcp_srv.close()

    def test_qq_admin_dm_commands_redact_secrets_and_block_groups(self) -> None:
        identities = IdentityRegistry(self.plane.store)
        admin = identities.create_user("Boss", role="admin")
        identities.bind(provider="qq", account_id="app-1", scope_id="*", external_id="qq-boss", user_id=admin.user_id)
        emp = identities.create_user("Alice", role="employee")
        identities.bind(provider="qq", account_id="app-1", scope_id="*", external_id="qq-alice", user_id=emp.user_id)

        api = _DummyQQApi()

        class StubWorkflow:
            def __init__(self, shared):
                self.shared_tools = shared
                self.recorded_events = []

            def record_event_context(self, event: Event) -> Event:
                self.recorded_events.append(event)
                self.shared_plane_ingest(event)
                return event

        wf = StubWorkflow(self.shared)
        wf.shared_plane_ingest = self.plane.ingest_event  # type: ignore[attr-defined]
        svc = QQChannelService(
            self.plane,
            wf,  # type: ignore[arg-type]
            api,
            QQBotSettings("app-1", "secret-1", allowed_publications=frozenset({"group_update", "suppress"})),
            tool_forge=self.forge,
            forge_secrets_vault=self.vault,
        )

        # Group attempts /forge-secret or /forge-approve -> strictly denied AND secret redacted from stored event & workflow context!
        raw_secret = "sk-never-persist-in-sqlite-or-upstream"
        svc.handle_gateway_event("GROUP_MESSAGE_CREATE", {
            "id": "m-grp-sec",
            "group_openid": "grp-1",
            "author": {"member_openid": "qq-boss"},
            "content": f"/forge-secret set LEAK_TEST {raw_secret}",
            "timestamp": "2026-11-21T00:00:01Z",
        })
        self.assertIn("私聊窗口", api.sent[-1][1])
        stored_evt = self.plane.get_event("qq:app-1:group:m-grp-sec")
        self.assertNotIn(raw_secret, stored_evt.content)
        self.assertNotIn(raw_secret, wf.recorded_events[-1].content)
        self.assertEqual(self.forge.list_secrets_metadata(), [])

        # Admin DM sets domain + secret (still redacted from SQLite & upstream context & reply!)
        svc.handle_gateway_event("C2C_MESSAGE_CREATE", {
            "id": "m-dm-dom",
            "author": {"user_openid": "qq-boss"},
            "content": "/forge-domain add api.example.com",
            "timestamp": "2026-11-21T00:00:02Z",
        })
        self.assertIn("api.example.com", api.sent[-1][1])

        svc.handle_gateway_event("C2C_MESSAGE_CREATE", {
            "id": "m-dm-sec",
            "author": {"user_openid": "qq-boss"},
            "content": f"/forge-secret set REAL_API_KEY {raw_secret}",
            "timestamp": "2026-11-21T00:00:03Z",
        })
        self.assertIn("REAL_API_KEY", api.sent[-1][1])
        self.assertNotIn(raw_secret, api.sent[-1][1])
        self.assertNotIn(raw_secret, self.plane.get_event("qq:app-1:c2c:m-dm-sec").content)
        self.assertNotIn(raw_secret, wf.recorded_events[-1].content)
        self.assertEqual(self.vault.get_map(["REAL_API_KEY"])["REAL_API_KEY"], raw_secret)

        # Employee DM cannot run /forge-approve
        self.forge.draft_tool(
            tool_id="dm_flow_tool",
            kind="http_api",
            description="QQ 私聊审批测试工具，供内部群查测试接口",
            target_visibility="internal",
            http_spec={"method": "GET", "url_template": "https://api.example.com/v1/echo"},
            allowed_domains=["api.example.com"],
        )
        self.forge.test_tool("dm_flow_tool")
        prop = self.forge.submit_release("dm_flow_tool")

        svc.handle_gateway_event("C2C_MESSAGE_CREATE", {
            "id": "m-emp-app",
            "author": {"user_openid": "qq-alice"},
            "content": f"/forge-approve {prop['proposal_id']}",
            "timestamp": "2026-11-21T00:00:04Z",
        })
        self.assertIn("权限不足", api.sent[-1][1])

        svc.handle_gateway_event("C2C_MESSAGE_CREATE", {
            "id": "m-dm-app",
            "author": {"user_openid": "qq-boss"},
            "content": f"/forge-approve {prop['proposal_id']}",
            "timestamp": "2026-11-21T00:00:05Z",
        })
        self.assertIn("已批准上线", api.sent[-1][1])
        self.assertEqual([t["tool_id"] for t in self.forge.list_active_tools_for_tier("internal")], ["dm_flow_tool"])

        # Assistant capability summary exposes tool_forge_enabled only to admin DM and custom_tools to internal/admin
        class DummyUpstream:
            pass

        seen_wf = DssuperAgentWorkflow(
            self.plane,
            DummyUpstream(),  # type: ignore[arg-type]
            shared_tools=self.shared,
            tool_forge=self.forge,
            mcp_servers_provider=lambda: build_forge_mcp_specs("127.0.0.1", 8768),
        )
        dm_caps = seen_wf._capability_summary(
            Event("e-adm", "qq.c2c", "dm", "dm", admin.user_id, "2026-11-21T00:00:06Z", "hi", [], ["dsh.task"])
        )
        self.assertTrue(dm_caps.get("tool_forge_enabled"))
        self.assertEqual(dm_caps["custom_tools"][0]["mcp_tool"], "mcp__forge_admin__dm_flow_tool")

        identities.register_group_seen("qq.group", "int-g")
        identities.update_group(source_type="qq.group", source_id="int-g", tier="internal")
        int_caps = seen_wf._capability_summary(
            Event("e-int", "qq.group", "int-g", "int-g", emp.user_id, "2026-11-21T00:00:07Z", "hi", [], ["dsh.task"])
        )
        self.assertNotIn("tool_forge_enabled", int_caps)
        self.assertEqual(int_caps["custom_tools"][0]["mcp_tool"], "mcp__forge_internal__dm_flow_tool")

        cli_caps = seen_wf._capability_summary(
            Event("e-cli", "qq.group", "cli-g", "cli-g", "cust-1", "2026-11-21T00:00:08Z", "hi", [], ["dsh.task"])
        )
        self.assertNotIn("custom_tools", cli_caps)

    def test_admin_api_forge_routes_end_to_end(self) -> None:
        token = "console-secret-token"
        handler = make_handler(
            self.plane,
            self.root,
            token,
            tool_forge=self.forge,
            forge_secrets_vault=self.vault,
        )
        srv = HTTPServer(("127.0.0.1", 0), handler)
        Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        hdr = {"Authorization": f"Bearer {token}"}
        try:
            st, dom, _ = _http_json(f"{base}/api/forge/domains", method="POST", headers=hdr, body={"domain": "api.example.com"})
            self.assertEqual(st, 201)
            self.assertEqual(dom["domain"], "api.example.com")

            st, sec, _ = _http_json(f"{base}/api/forge/secrets", method="POST", headers=hdr, body={"name": "MY_TOK", "value": "secret-abc-999"})
            self.assertEqual(st, 201)
            self.assertEqual(sec["name"], "MY_TOK")

            _http_json(
                f"{base}/api/forge/tools/draft",
                method="POST",
                headers=hdr,
                body={
                    "tool_id": "console_api_tool",
                    "kind": "http_api",
                    "description": "从控制台接口测试锻造链路与脱敏显示",
                    "target_visibility": "internal",
                    "http_spec": {
                        "method": "GET",
                        "url_template": "https://api.example.com/v1/echo",
                        "headers": {"Authorization": "Bearer {{MY_TOK}}"},
                    },
                    "allowed_domains": ["api.example.com"],
                },
            )
            _, rep, _ = _http_json(f"{base}/api/forge/tools/test", method="POST", headers=hdr, body={"tool_id": "console_api_tool"})
            self.assertTrue(rep["ok"])
            self.assertNotIn("secret-abc-999", json.dumps(rep))

            _, prop, _ = _http_json(f"{base}/api/forge/tools/submit", method="POST", headers=hdr, body={"tool_id": "console_api_tool"})
            _, app_res, _ = _http_json(f"{base}/api/forge/proposals/approve", method="POST", headers=hdr, body={"proposal_id": prop["proposal_id"]})
            self.assertEqual(app_res["active_version"], 1)

            _, summary, _ = _http_json(f"{base}/api/forge/summary", method="GET", headers=hdr)
            self.assertEqual(summary["pending_proposals"], 0)
            self.assertEqual(len(summary["tools"]), 1)
            self.assertNotIn("secret-abc-999", json.dumps(summary))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_parse_forge_mcp_settings_validation(self) -> None:
        self.assertEqual(parse_forge_mcp_settings({}), (True, "127.0.0.1", 28768, "admin"))
        with self.assertRaises(ValueError):
            parse_forge_mcp_settings({"COMPANY_AGENT_FORGE_MCP_HOST": "0.0.0.0"})
        with self.assertRaises(ValueError):
            parse_forge_mcp_settings({"COMPANY_AGENT_FORGE_MCP_OWNER": "worker"})
