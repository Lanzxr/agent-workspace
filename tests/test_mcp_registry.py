from __future__ import annotations

import json
import os
import shlex
import shutil
import tempfile
import unittest
from dataclasses import replace
from http.server import HTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.admin_api import make_handler
from app.assistant_workflow import DssuperAgentWorkflow
from app.control_plane import ControlPlane
from app.dsh import DshAdapter
from app.dssuperagent import DssuperAgentDecision, DssuperAgentReceipt
from app.identity import IdentityRegistry
from app.mcp_registry import (
    McpConfigError,
    McpServerSpec,
    all_mcp_secret_env_names,
    describe_mcp_servers,
    merge_mcp_servers,
    parse_mcp_servers_json,
    tier_allows_mcp,
)
from app.models import ExecutionRequest
from app.runtime_config import RuntimeConfig
from app.workspace import WorkspaceManager


class McpRegistryAndIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plane = ControlPlane.open(self.root / "control.sqlite")
        self.manager = WorkspaceManager(self.root / "workspaces")
        self.ws = self.manager.provision("ws-mcp-1")
        self.bwrap = self.root / "bwrap"
        self.bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        if os.name != "nt":
            self.bwrap.chmod(0o700)

    def tearDown(self) -> None:
        self.plane.store.close()
        self.tmp.cleanup()

    def test_mcp_spec_validation_rejects_inline_secrets_bad_urls_and_reserved_vars(self) -> None:
        spec = McpServerSpec.from_dict({
            "server_id": "biz_ro",
            "description": "只读查询业务余额与用量",
            "transport": "stdio",
            "access_level": "internal",
            "command": "python3",
            "args": ["/opt/mcp/biz_ro.py"],
            "env_refs": {"SERVICE_API_KEY": "COMPANY_AGENT_MCP_SECRET_BIZ_RO"},
        })
        self.assertEqual(spec.server_id, "biz_ro")
        self.assertEqual(spec.access_level, "internal")
        self.assertEqual(spec.referenced_host_env_vars(), ("COMPANY_AGENT_MCP_SECRET_BIZ_RO",))

        http_spec = McpServerSpec.from_dict({
            "server_id": "billing_ro",
            "transport": "streamable-http",
            "access_level": "admin",
            "url": "http://127.0.0.1:3900/mcp",
            "header_env_refs": {"Authorization": "Bearer ${COMPANY_AGENT_MCP_SECRET_BILLING}"},
        })
        self.assertEqual(http_spec.referenced_host_env_vars(), ("COMPANY_AGENT_MCP_SECRET_BILLING",))

        # Bare env var in header_env_refs + WSL host bridge aliases/private IPs validate cleanly
        http_bare = McpServerSpec.from_dict({
            "server_id": "bare_hdr",
            "transport": "streamable-http",
            "url": "https://mcp.internal.example/mcp",
            "header_env_refs": {"X-Api-Key": "COMPANY_AGENT_MCP_SECRET_BARE"},
        })
        self.assertEqual(dict(http_bare.header_env_refs)["X-Api-Key"], "${COMPANY_AGENT_MCP_SECRET_BARE}")
        McpServerSpec.from_dict({"server_id": "wsl_alias", "transport": "streamable-http", "url": "http://wsl-host:18766/mcp"})
        McpServerSpec.from_dict({"server_id": "wsl_nat_ip", "transport": "streamable-http", "url": "http://172.23.112.1:18766/mcp"})

        invalid_payloads = [
            {"server_id": "bad id!", "transport": "stdio", "command": "echo"},
            {"server_id": "bad_tier", "transport": "stdio", "access_level": "root", "command": "echo"},
            {"server_id": "inline_env", "transport": "stdio", "command": "echo", "env": {"API_TOKEN": "sk-123"}},
            {"server_id": "inline_hdr", "transport": "streamable-http", "url": "https://a.test/mcp",
             "headers": {"Authorization": "Bearer raw-secret"}},
            {"server_id": "reserved_ref", "transport": "stdio", "command": "echo",
             "env_refs": {"KEY": "QQBOT_CLIENT_SECRET"}},
            {"server_id": "reserved_hdr", "transport": "streamable-http", "url": "https://a.test/mcp",
             "header_env_refs": {"Authorization": "Bearer ${COMPANY_AGENT_ADMIN_TOKEN}"}},
            {"server_id": "plain_http_public", "transport": "streamable-http", "url": "http://api.external.example/mcp"},
            {"server_id": "link_local_metadata", "transport": "streamable-http", "url": "http://169.254.169.254/latest/meta-data"},
            {"server_id": "unspecified_bind", "transport": "streamable-http", "url": "http://0.0.0.0:18766/mcp"},
            {"server_id": "cred_url", "transport": "streamable-http", "url": "https://u:p@a.test/mcp"},
        ]
        for bad in invalid_payloads:
            with self.subTest(bad=bad["server_id"]):
                with self.assertRaises(McpConfigError):
                    McpServerSpec.from_dict(bad)

    def test_tier_access_control_fail_closed_across_levels(self) -> None:
        self.assertTrue(tier_allows_mcp("common", "client_group"))
        self.assertTrue(tier_allows_mcp("common", "direct"))
        self.assertTrue(tier_allows_mcp("common", "internal"))
        self.assertTrue(tier_allows_mcp("common", "admin"))

        self.assertFalse(tier_allows_mcp("internal", "client_group"))
        self.assertFalse(tier_allows_mcp("internal", "direct"))
        self.assertTrue(tier_allows_mcp("internal", "internal"))
        self.assertTrue(tier_allows_mcp("internal", "admin"))

        self.assertFalse(tier_allows_mcp("admin", "client_group"))
        self.assertFalse(tier_allows_mcp("admin", "direct"))
        self.assertFalse(tier_allows_mcp("admin", "internal"))
        self.assertTrue(tier_allows_mcp("admin", "admin"))

        self.assertFalse(tier_allows_mcp("internal", "unknown_tier"))
        self.assertFalse(tier_allows_mcp("unknown_level", "admin"))

    def test_runtime_config_parses_mcp_servers_and_redacts_secrets(self) -> None:
        env_path = self.root / "runtime.env"
        mcp_json = json.dumps([
            {
                "server_id": "biz_ro",
                "description": "Business system read-only queries",
                "transport": "stdio",
                "access_level": "internal",
                "command": "python3",
                "args": ["/opt/biz_ro_mcp.py"],
                "env_refs": {"SERVICE_API_KEY": "COMPANY_AGENT_MCP_SECRET_BIZ_RO"},
            }
        ])
        env_path.write_text(
            "\n".join([
                "AGENT_MODEL_URL=https://api.example.com/v1",
                "AGENT_MODEL_API_KEY=model-secret-value-111",
                "AGENT_MODEL_NAME=deepseek-v4.1-flash",
                f"COMPANY_AGENT_MCP_SERVERS={mcp_json}",
                "COMPANY_AGENT_MCP_SECRET_BIZ_RO=biz-super-secret-xyz",
                "COMPANY_AGENT_MCP_SECRET_EXTRA=extra-secret-999",
            ]),
            encoding="utf-8",
        )
        cfg = RuntimeConfig.load(env_path)
        self.assertEqual(len(cfg.mcp_servers), 1)
        self.assertEqual(cfg.mcp_servers[0].server_id, "biz_ro")
        self.assertNotIn("biz-super-secret-xyz", repr(cfg))
        redacted = cfg.redact("Leak test model-secret-value-111 and biz-super-secret-xyz + extra-secret-999")
        self.assertNotIn("model-secret-value-111", redacted)
        self.assertNotIn("biz-super-secret-xyz", redacted)
        self.assertNotIn("extra-secret-999", redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 3)

    def test_dsh_provider_patch_env_injection_and_bwrap_unsetenv_enforce_tier_isolation(self) -> None:
        common_mcp = McpServerSpec.from_dict({
            "server_id": "public_faq",
            "description": "Public read-only docs",
            "transport": "streamable-http",
            "access_level": "common",
            "url": "http://127.0.0.1:4101/mcp",
            "header_env_refs": {"Authorization": "Bearer ${COMPANY_AGENT_MCP_SECRET_PUBLIC}"},
        })
        internal_mcp = McpServerSpec.from_dict({
            "server_id": "biz_ro",
            "description": "Internal business query",
            "transport": "stdio",
            "access_level": "internal",
            "command": "python3",
            "args": ["/opt/mcp/biz_ro.py"],
            "env_refs": {"SERVICE_API_KEY": "COMPANY_AGENT_MCP_SECRET_INTERNAL"},
        })
        admin_mcp = McpServerSpec.from_dict({
            "server_id": "admin_ops",
            "description": "Admin-only billing query",
            "transport": "stdio",
            "access_level": "admin",
            "command": "python3",
            "args": ["/opt/mcp/admin.py"],
            "env_refs": {"ADMIN_OPS_TOKEN": "COMPANY_AGENT_MCP_SECRET_ADMIN"},
        })
        disabled_mcp = McpServerSpec.from_dict({
            "server_id": "disabled_svc",
            "enabled": False,
            "transport": "stdio",
            "access_level": "common",
            "command": "echo",
        })
        cfg = RuntimeConfig(
            model_url="https://api.example.com/v1",
            model_name="deepseek-v4.1-flash",
            api_key="model-secret",
            dsh_command=("dsh",),
            mcp_servers=(common_mcp, internal_mcp, admin_mcp, disabled_mcp),
            mcp_secrets={
                "COMPANY_AGENT_MCP_SECRET_PUBLIC": "secret-public-111",
                "COMPANY_AGENT_MCP_SECRET_INTERNAL": "secret-internal-222",
                "COMPANY_AGENT_MCP_SECRET_ADMIN": "secret-admin-333",
            },
        )
        adapter = DshAdapter(cfg, self.manager, self.root / "runtime", bubblewrap_executable=self.bwrap)
        runtime_home = self.root / "runtime" / "dsh" / self.ws.workspace_id
        runtime_home.mkdir(parents=True)

        # 1) Provider patches must respect tier and NEVER contain secret values
        client_patch = adapter._build_provider_patch(runtime_home / "r", web_enabled=False, workspace_tier="client_group")
        client_ids = [item["id"] for blk in client_patch if "insert" in blk for item in blk["insert"]]
        self.assertEqual(client_ids, ["company-agent-mcp-public_faq"])
        self.assertNotIn("secret-public-111", json.dumps(client_patch))

        internal_patch = adapter._build_provider_patch(runtime_home / "r", web_enabled=False, workspace_tier="internal")
        internal_ids = [item["id"] for blk in internal_patch if "insert" in blk for item in blk["insert"]]
        self.assertEqual(internal_ids, ["company-agent-mcp-public_faq", "company-agent-mcp-biz_ro"])
        self.assertNotIn("secret-internal-222", json.dumps(internal_patch))

        admin_patch = adapter._build_provider_patch(runtime_home / "r", web_enabled=False, workspace_tier="admin")
        admin_ids = [item["id"] for blk in admin_patch if "insert" in blk for item in blk["insert"]]
        self.assertEqual(admin_ids, ["company-agent-mcp-public_faq", "company-agent-mcp-biz_ro", "company-agent-mcp-admin_ops"])
        self.assertNotIn("secret-admin-333", json.dumps(admin_patch))

        # Wrapup patch excludes MCP completely
        wrapup_patch = adapter._build_provider_patch(runtime_home / "r", web_enabled=False, workspace_tier="admin", include_mcp=False)
        self.assertFalse(any("insert" in blk for blk in wrapup_patch))

        # 2) Host runtime environment only injects secrets authorized for THIS tier
        env_client = {"COMPANY_AGENT_MCP_SECRET_ADMIN": "stale-leak"}
        adapter._apply_runtime_environment(env_client, web_enabled=False, workspace_tier="client_group")
        self.assertEqual(env_client.get("COMPANY_AGENT_MCP_SECRET_PUBLIC"), "secret-public-111")
        self.assertNotIn("COMPANY_AGENT_MCP_SECRET_INTERNAL", env_client)
        self.assertNotIn("COMPANY_AGENT_MCP_SECRET_ADMIN", env_client)

        env_internal: dict[str, str] = {}
        adapter._apply_runtime_environment(env_internal, web_enabled=False, workspace_tier="internal")
        self.assertEqual(env_internal.get("COMPANY_AGENT_MCP_SECRET_INTERNAL"), "secret-internal-222")
        self.assertNotIn("COMPANY_AGENT_MCP_SECRET_ADMIN", env_internal)

        # 3) Bubblewrap runner clears ALL MCP secrets from sandboxed bash subprocesses + keeps --unshare-net
        runner_script = adapter._write_sandbox_runner(runtime_home, self.ws.root, workspace_tier="admin").read_text(encoding="utf-8")
        self.assertIn("--unshare-net", runner_script)
        for env_var in all_mcp_secret_env_names(cfg.mcp_servers, cfg.mcp_secrets):
            self.assertIn(f"--unsetenv {shlex.quote(env_var)}", runner_script)

        # 4) Execution contract advertises active MCP tools without leaking paths or secrets
        req = ExecutionRequest(
            "exec-1", "work-1", "step-1", self.ws.workspace_id, "dsh.task",
            {"prompt": "check telemetry"}, "queued", "2026-11-20T00:00:00Z", "2026-11-20T00:00:00Z",
        )
        task = adapter.preflight(req, self.ws)
        contract = adapter._build_execution_contract(req, task, web_enabled=False, mcp_servers=(internal_mcp,))
        self.assertIn("mcp__biz_ro__*", contract)
        self.assertNotIn("COMPANY_AGENT_MCP_SECRET_INTERNAL", contract)
        self.assertNotIn("/opt/mcp/biz_ro.py", contract)

    def test_workflow_capability_summary_filters_mcp_servers_and_hides_secrets_and_commands(self) -> None:
        identities = IdentityRegistry(self.plane.store)
        identities.create_user(user_id="admin-u", display_name="管理员", role="admin")
        identities.bind(provider="qq", account_id="bot1", scope_id="*", external_id="qq-admin", user_id="admin-u")
        identities.update_group(source_type="qq.group", source_id="grp-int", display_name="内部群", tier="internal")
        identities.update_group(source_type="qq.group", source_id="grp-ext", display_name="客户群", tier="client_group")

        mcp_list = (
            McpServerSpec.from_dict({
                "server_id": "biz_ro",
                "description": "Business query",
                "transport": "stdio",
                "access_level": "internal",
                "command": "/usr/local/bin/secret-path.py",
                "env_refs": {"API_KEY": "COMPANY_AGENT_MCP_SECRET_BIZ_RO"},
            }),
            McpServerSpec.from_dict({
                "server_id": "admin_vault",
                "description": "Admin vault read",
                "transport": "streamable-http",
                "access_level": "admin",
                "url": "http://127.0.0.1:9444/mcp",
                "header_env_refs": {"Authorization": "Bearer ${COMPANY_AGENT_MCP_SECRET_VAULT}"},
            }),
        )
        seen_caps: list[dict] = []

        class Upstream:
            def submit_event(self, event, *, context=None, interaction_mode=None, available_capabilities=None):
                seen_caps.append(available_capabilities or {})
                return DssuperAgentReceipt("stored", f"trig:{event.event_id}", f"thr:{event.conversation_id}")

            def wait_for_decision(self, thread_key, trigger_id, *, timeout=120):
                return DssuperAgentDecision(
                    f"dec-{trigger_id}", [trigger_id], thread_key, "ignore", "ignore", [], "2026-11-20T00:00:00Z",
                    "inferred",
                    assistant_intent={
                        "action": "observe",
                        "reason": "ok",
                        "publication": {"recommendation": "suppress"},
                    },
                )

        workflow = DssuperAgentWorkflow(self.plane, Upstream(), mcp_servers_provider=lambda: mcp_list)  # type: ignore[arg-type]

        from app.models import Event

        # L1 client group sees zero internal/admin MCP servers
        ev_client = Event("ev-c1", "qq.group", "grp-ext", "qq:bot1:group:grp-ext", "qq-guest", "2026-11-20T00:00:00Z", "hi", [], ["dsh.task"])
        self.plane.ingest_event(ev_client)
        workflow.process_event(ev_client)
        self.assertEqual(seen_caps[-1]["mcp_servers"], [])

        # L2 internal group sees only internal MCP, never admin MCP or internal paths/env names
        ev_int = Event("ev-i1", "qq.group", "grp-int", "qq:bot1:group:grp-int", "qq-guest", "2026-11-20T00:00:00Z", "hi", [], ["dsh.task"])
        self.plane.ingest_event(ev_int)
        workflow.process_event(ev_int)
        self.assertEqual([s["id"] for s in seen_caps[-1]["mcp_servers"]], ["biz_ro"])
        caps_json = json.dumps(seen_caps[-1], ensure_ascii=False)
        self.assertNotIn("COMPANY_AGENT_MCP_SECRET_BIZ_RO", caps_json)
        self.assertNotIn("secret-path.py", caps_json)

        # L3 admin DM sees both internal + admin MCP
        ev_adm = Event("ev-a1", "qq.c2c", "admin-dm", "qq:bot1:c2c:qq-admin", "admin-u", "2026-11-20T00:00:00Z", "hi", [], ["dsh.task"])
        self.plane.ingest_event(ev_adm)
        workflow.process_event(ev_adm)
        self.assertEqual([s["id"] for s in seen_caps[-1]["mcp_servers"]], ["biz_ro", "admin_vault"])

    def test_admin_api_mcp_servers_crud_rejects_secrets_and_audits(self) -> None:
        runtime_mcp = parse_mcp_servers_json(json.dumps([
            {
                "server_id": "biz_ro",
                "description": "runtime baseline",
                "transport": "stdio",
                "access_level": "admin",
                "command": "python3",
                "args": ["/opt/biz_ro.py"],
                "env_refs": {"SERVICE_API_KEY": "COMPANY_AGENT_MCP_SECRET_BIZ_RO"},
            }
        ]))
        secrets_map = {"COMPANY_AGENT_MCP_SECRET_BIZ_RO": "top-secret-service-token"}
        server = HTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                self.plane, self.root, "tok-admin-secret-1234567890",
                mcp_runtime_servers=runtime_mcp, mcp_secrets=secrets_map,
            ),
        )
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        headers = {"Authorization": "Bearer tok-admin-secret-1234567890", "Content-Type": "application/json"}
        try:
            with urlopen(Request(base + "/api/mcp-servers", headers=headers)) as r:
                body = r.read().decode("utf-8")
                self.assertNotIn("top-secret-service-token", body)
                data = json.loads(body)
                self.assertEqual(len(data["servers"]), 1)
                self.assertEqual(data["servers"][0]["missing_secret_vars"], [])

            # Reject inline credential literals
            with self.assertRaises(HTTPError) as err_ctx:
                urlopen(Request(
                    base + "/api/mcp-servers",
                    data=json.dumps({
                        "server_id": "bad_srv",
                        "transport": "stdio",
                        "command": "echo",
                        "env": {"TOKEN": "plain-literal"},
                    }).encode(),
                    headers=headers,
                    method="PUT",
                ))
            self.assertEqual(err_ctx.exception.code, 400)
            err_ctx.exception.close()

            # Partial override toggling runtime server to internal + disabled
            with urlopen(Request(
                base + "/api/mcp-servers",
                data=json.dumps({"server_id": "biz_ro", "access_level": "internal", "enabled": False}).encode(),
                headers=headers,
                method="PUT",
            )) as r:
                updated = json.loads(r.read())["servers"][0]
                self.assertEqual(updated["source"], "runtime+override")
                self.assertEqual(updated["access_level"], "internal")
                self.assertFalse(updated["enabled"])

            # Add console HTTP MCP server
            with urlopen(Request(
                base + "/api/mcp-servers",
                data=json.dumps({
                    "server_id": "metrics_http",
                    "description": "Local metrics HTTP MCP",
                    "transport": "streamable-http",
                    "access_level": "internal",
                    "url": "http://127.0.0.1:4200/mcp",
                    "header_env_refs": {"Authorization": "Bearer ${COMPANY_AGENT_MCP_SECRET_METRICS}"},
                }).encode(),
                headers=headers,
                method="PUT",
            )) as r:
                listed = json.loads(r.read())["servers"]
                self.assertEqual(len(listed), 2)
                metrics_row = next(s for s in listed if s["server_id"] == "metrics_http")
                self.assertEqual(metrics_row["missing_secret_vars"], ["COMPANY_AGENT_MCP_SECRET_METRICS"])

            # Reset runtime override cleanly
            with urlopen(Request(base + "/api/mcp-servers?server_id=biz_ro", headers=headers, method="DELETE")) as r:
                reset_row = next(s for s in json.loads(r.read())["servers"] if s["server_id"] == "biz_ro")
                self.assertEqual(reset_row["source"], "runtime")
                self.assertTrue(reset_row["enabled"])
                self.assertEqual(reset_row["access_level"], "admin")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_controlled_mcp_plugin_resolves_env_and_headers_with_real_dsh_package(self) -> None:
        node = shutil.which("node")
        if not node:
            for candidate in sorted(Path.home().glob("node-v24.*-linux-x64/bin/node")):
                node = str(candidate)
                break
        mcp_clients = sorted(Path.home().glob(".npm-company-agent/_npx/*/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js"))
        cordis_mods = sorted(Path.home().glob(".npm-company-agent/_npx/*/node_modules/@deepseek-ai/cordis/lib/index.js"))
        if not node or not mcp_clients or not cordis_mods:
            self.skipTest("Node 24, Cordis, or @deepseek-ai/dsh-mcp-client runtime package not present")
        plugin_path = Path(__file__).resolve().parents[1] / "app" / "dsh_plugins" / "controlled-mcp.mjs"
        stub_srv = self.root / "stub_mcp_server.mjs"
        stub_srv.write_text(
            """
import readline from "node:readline";
const seenEnv = process.env.UPSTREAM_TOKEN || "";
const leakedPlatformEnv = process.env.COMPANY_AGENT_MODEL_API_KEY || "";
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  if (!line.trim()) return;
  const req = JSON.parse(line);
  if (req.method === "initialize") {
    process.stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: req.id,
      result: {
        protocolVersion: "2025-03-26",
        capabilities: { tools: {} },
        serverInfo: { name: "stub-mcp", version: "1.0.0" },
      },
    }) + "\\n");
  } else if (req.method === "tools/list") {
    process.stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: req.id,
      result: {
        tools: [{
          name: "ping",
          description: `env_ok=${seenEnv === "secret-demo-token" && !leakedPlatformEnv}`,
          inputSchema: { type: "object", properties: {} },
        }],
      },
    }) + "\\n");
  } else if (req.id !== undefined) {
    process.stdout.write(JSON.stringify({
      jsonrpc: "2.0",
      id: req.id,
      error: { code: -32601, message: "Method not found" },
    }) + "\\n");
  }
});
rl.on("close", () => process.exit(0));
""".strip(),
            encoding="utf-8",
        )
        test_js = self.root / "verify_controlled_mcp.mjs"
        test_js.write_text(
            f"""
import {{ Context, Service }} from {json.dumps(cordis_mods[-1].as_uri())};
import * as controlledPlugin from {json.dumps(plugin_path.as_uri())};

// 1) Optional missing env var warns without crashing startup
const warnings = [];
const softCtx = {{ logger: {{ warn: (msg) => warnings.push(msg) }}, tools: {{ register: () => () => {{}} }} }};
await controlledPlugin.apply(softCtx, {{
  serverName: "missing_env_optional",
  transport: "stdio",
  command: {json.dumps(node)},
  envRefs: {{ UPSTREAM_KEY: "NONEXISTENT_ENV_VAR_ABC" }},
  failOnStartupError: false,
  mcpClientModulePath: {json.dumps(str(mcp_clients[-1]))},
}});
if (!warnings.some(w => w.includes("NONEXISTENT_ENV_VAR_ABC"))) {{
  throw new Error("expected missing-env warning without crashing optional MCP");
}}

// 2) Real Cordis context + real stdio JSON-RPC MCP tool discovery & secret envRefs isolation
const registered = [];
class DummyTools extends Service {{
  constructor(ctx) {{ super(ctx, "tools"); }}
  register(def) {{ registered.push(def); return () => {{}}; }}
}}
const root = new Context();
const toolsFork = root.plugin(DummyTools);
await toolsFork.await();
const mcpFork = root.plugin(controlledPlugin, {{
  serverName: "demo_ro",
  transport: "stdio",
  command: {json.dumps(node)},
  args: [{json.dumps(str(stub_srv))}],
  envRefs: {{ UPSTREAM_TOKEN: "TEST_MCP_SECRET_DEMO" }},
  failOnStartupError: true,
  mcpClientModulePath: {json.dumps(str(mcp_clients[-1]))},
}});
await mcpFork.await();
if (!registered.some(t => t.name === "mcp__demo_ro__ping" && t.description === "env_ok=true")) {{
  throw new Error("unexpected registered tools: " + JSON.stringify(registered));
}}
await mcpFork.dispose();
await toolsFork.dispose();

// 3) wsl-host alias resolves Linux /proc/net/route gateway IP dynamically
const fakeRoute = {json.dumps(str(self.root / "route.txt"))};
const fsMod = await import("node:fs");
fsMod.writeFileSync(fakeRoute, "Iface\\tDestination\\tGateway\\tFlags\\neth0\\t00000000\\t017017AC\\t0003\\n");
const resolvedUrl = controlledPlugin.resolveBridgeHostUrl("http://wsl-host:18768/mcp", fakeRoute);
if (resolvedUrl !== "http://172.23.112.1:18768/mcp") {{
  throw new Error("expected resolved gateway URL, got: " + resolvedUrl);
}}
console.log("CONTROLLED_MCP_PLUGIN_OK");
""".strip(),
            encoding="utf-8",
        )
        import subprocess
        env = dict(os.environ)
        env["TEST_MCP_SECRET_DEMO"] = "secret-demo-token"
        env["COMPANY_AGENT_MODEL_API_KEY"] = "must-not-leak-to-mcp-child"
        res = subprocess.run([node, str(test_js)], env=env, capture_output=True, text=True, check=False, timeout=15)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("CONTROLLED_MCP_PLUGIN_OK", res.stdout)


if __name__ == "__main__":
    unittest.main()
