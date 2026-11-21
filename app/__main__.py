from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from uuid import uuid4

from .assistant_workflow import AssistantPolicy, DssuperAgentWorkflow
from .attachments import AttachmentIngestor
from .admin_api import serve as serve_admin
from .control_plane import ControlPlane, ControlPlaneError, NotFound
from .dsh import DshAdapter
from .dssuperagent import DssuperAgentAdapter, DssuperAgentError
from .dssuperagent import DEFAULT_DECISION_TIMEOUT_SECONDS
from .execution import ExecutionDispatcher
from .execution_state import ExecutionBusy, private_directory
from .knowledge.config import KnowledgeSettings
from .knowledge.http_api import KnowledgeQueryServer
from .knowledge.service import KnowledgeService
from .mcp_registry import McpServerSpec, load_mcp_secrets, merge_mcp_servers, parse_mcp_servers_json
from .models import Event, PlanStep
from .ports import assert_port_available, resolve_port_settings
from .qq_channel import DEFAULT_AMBIENT_COOLDOWN_SECONDS, DEFAULT_BOT_ALIASES, QQBotAPI, QQChannelService, QQGateway
from .runtime_config import QQBotSettings, RuntimeConfig, parse_forge_mcp_settings
from .scheduler import ScheduleService, SchedulerWorker
from .scheduled_resume import ScheduledResumeHandler
from .shared_tools import SharedToolsManager
from .tool_forge import (
    ForgeSecretsVault,
    ToolForgeService,
    build_forge_mcp_specs,
    ensure_forge_mcp_tokens,
    forge_mcp_secrets_map,
)
from .tool_forge_mcp import ToolForgeMcpServer
from .workspace import WorkspaceError, WorkspaceManager
from .workspace_retention import (DEFAULT_WORKSPACE_RETENTION_DAYS, get_retention_days,
                                  prune_expired_workspace_files)
from .web_gate import WebGateServer, WebGateSettings


def _start_web_gate(plane, config: Path, settings: WebGateSettings) -> WebGateServer:
    assert_port_available(settings.host, settings.port, service_name="Web Gate", env_var="COMPANY_AGENT_WEB_GATE_PORT")
    server = WebGateServer(
        settings,
        enabled=lambda: bool(plane.get_setting("dssuperagent_web_search_enabled",
                                                _initial_flag(config, "AGENT_DSSUPERAGENT_WEB_SEARCH_ENABLED"))),
        dsh_enabled=lambda: bool(plane.get_setting("web_search_enabled",
                                                    _initial_flag(config, "AGENT_WEB_SEARCH_ENABLED"))),
        audit=lambda action, payload: plane.record_external_audit(action, payload),
    )
    server.start()
    return server


def _configured_owner(config: Path, name: str, default: str) -> str:
    owner = RuntimeConfig._parse_env(config).get(name, default).strip().lower()
    if owner not in {"admin", "qq"}:
        raise ValueError(f"{name} must be admin or qq")
    return owner


def main() -> int:
    parser = argparse.ArgumentParser(description="Company Agent development execution console")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/company-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    workspace_command = commands.add_parser("workspace", help="Create or inspect an Agent workspace")
    workspace_command.add_argument("workspace_id")
    submit = commands.add_parser("submit", help="Explicitly authorize a low-risk local development task")
    submit.add_argument("--workspace", required=True)
    submit.add_argument("--task-file", type=Path, required=True)
    submit.add_argument("--artifact", action="append", default=[])
    submit.add_argument("--timeout", type=float, default=3600)
    submit.add_argument("--execution-id", default=None)
    execute = commands.add_parser("execute", help="Dispatch or reconcile an existing Execution Request")
    execute.add_argument("execution_id")
    execute.add_argument("--config", type=Path, default=Path.home() / ".config/company-agent/runtime.env")
    result = commands.add_parser("result", help="Read a persisted result without calling the model")
    result.add_argument("execution_id")
    assistant_event = commands.add_parser("assistant-event", help="Send a standard event through DSSuperAgent")
    assistant_event.add_argument("--event-file", type=Path, required=True)
    assistant_event.add_argument("--dssuperagent-url", default=None)
    assistant_event.add_argument("--workspace")
    assistant_event.add_argument("--auto-execute-capability", action="append", default=[])
    assistant_event.add_argument("--config", type=Path, default=Path.home() / ".config/company-agent/runtime.env")
    assistant_event.add_argument("--decision-timeout", type=float, default=DEFAULT_DECISION_TIMEOUT_SECONDS)
    qq_serve = commands.add_parser("qq-serve", help="Receive QQ group events through Company Agent")
    qq_serve.add_argument("--config", type=Path, default=Path.home() / ".config/company-agent/runtime.env")
    qq_serve.add_argument("--dssuperagent-url", default=None)
    qq_serve.add_argument("--decision-timeout", type=float, default=DEFAULT_DECISION_TIMEOUT_SECONDS)
    admin = commands.add_parser("admin-serve", help="Run the authenticated local control console")
    admin.add_argument("--host", default=None)
    admin.add_argument("--port", type=int, default=None)
    admin.add_argument("--config", type=Path, default=Path.home() / ".config/company-agent/runtime.env")
    admin.add_argument("--dssuperagent-url", default=None)
    admin.add_argument("--decision-timeout", type=float, default=DEFAULT_DECISION_TIMEOUT_SECONDS)
    outbox = commands.add_parser("outbox", help="Inspect or manually resolve uncertain outbound messages")
    outbox_commands = outbox.add_subparsers(dest="outbox_command", required=True)
    outbox_commands.add_parser("list", help="List outbound message states")
    resolve_outbox = outbox_commands.add_parser("resolve", help="Confirm or retry a message left in sending state")
    resolve_outbox.add_argument("outbox_id")
    resolution = resolve_outbox.add_mutually_exclusive_group(required=True)
    resolution.add_argument("--delivered", action="store_true", help="Confirm QQ received the message")
    resolution.add_argument("--retry", action="store_true", help="Confirm it was not delivered and retry")
    arguments = parser.parse_args()
    plane = None
    active_query_server = None
    active_web_gate = None
    active_forge_mcp = None
    try:
        data_directory = private_directory(arguments.data_dir.expanduser().resolve())
        if arguments.command == "workspace":
            manager = WorkspaceManager(data_directory / "workspaces")
            manager.provision(arguments.workspace_id)
            print(json.dumps(manager.summary(arguments.workspace_id), ensure_ascii=False, indent=2))
            return 0
        plane = ControlPlane.open(data_directory / "control.sqlite")
        if arguments.command == "admin-serve":
            configured = RuntimeConfig._parse_env(arguments.config).get("COMPANY_AGENT_ADMIN_TOKEN", "")
            token = (os.environ.get("COMPANY_AGENT_ADMIN_TOKEN") or configured).strip()
            if len(token) < 24:
                raise ValueError("set COMPANY_AGENT_ADMIN_TOKEN to a random value of at least 24 characters")
            scheduler_shared = SharedToolsManager(data_directory / "shared-tools")
            scheduler_shared.ensure_layers()
            _ensure_default_settings(plane)
            settings = QQBotSettings.load(arguments.config)
            knowledge_service = _load_knowledge_service(plane, arguments.config, data_directory)
            api = QQBotAPI(settings)
            scheduler_plane = ControlPlane.open(data_directory / "control.sqlite")
            scheduler_vault, scheduler_forge = _build_tool_forge(scheduler_plane, data_directory, scheduler_shared)
            ports = _resolved_ports(arguments.config)
            dss_url = arguments.dssuperagent_url or ports.dssuperagent_url
            admin_host = arguments.host or ports.admin_host
            admin_port = arguments.port if arguments.port is not None else ports.admin_port
            workflow = _qq_workflow(scheduler_plane, data_directory, arguments.config,
                                    dss_url, arguments.decision_timeout,
                                    settings, knowledge_service, tool_forge=scheduler_forge)
            channel = QQChannelService(
                scheduler_plane,
                workflow,
                api,
                settings,
                knowledge_service=knowledge_service,
                workspace_manager=getattr(workflow, "manager", None),
                tool_forge=scheduler_forge,
                forge_secrets_vault=scheduler_vault,
            )
            worker = SchedulerWorker(ScheduleService(scheduler_plane.store), ScheduledResumeHandler(channel))
            channel.flush_outbox()
            gate_settings = WebGateSettings.load(arguments.config)
            if gate_settings.owner == "admin":
                active_web_gate = _start_web_gate(plane, arguments.config, gate_settings)
            if _configured_owner(arguments.config, "COMPANY_AGENT_KNOWLEDGE_API_OWNER", "qq") == "admin" and knowledge_service:
                active_query_server = _start_knowledge_server(knowledge_service)
            admin_vault, admin_forge = _build_tool_forge(plane, data_directory, scheduler_shared)
            if _configured_owner(arguments.config, "COMPANY_AGENT_FORGE_MCP_OWNER", "admin") == "admin":
                active_forge_mcp = _start_forge_mcp(plane, arguments.config, data_directory, scheduler_shared)
            mcp_servers, mcp_secrets = _load_runtime_mcp(arguments.config, data_directory)
            serve_admin(plane, data_directory, admin_host, admin_port, token, worker,
                        knowledge_service=knowledge_service, qq_app_id=settings.app_id,
                        mcp_runtime_servers=mcp_servers, mcp_secrets=mcp_secrets,
                        tool_forge=admin_forge, forge_secrets_vault=admin_vault)
            return 0
        if arguments.command == "outbox":
            if arguments.outbox_command == "list":
                print(json.dumps([message.to_dict() for message in plane.list_outbox()], ensure_ascii=False, indent=2))
                return 0
            message = plane.resolve_outbox_delivery(arguments.outbox_id, delivered=arguments.delivered)
            print(json.dumps(message.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if arguments.command == "qq-serve":
            qq_shared = SharedToolsManager(data_directory / "shared-tools")
            qq_shared.ensure_layers()
            _ensure_default_settings(plane)
            settings = QQBotSettings.load(arguments.config)
            knowledge_service = _load_knowledge_service(plane, arguments.config, data_directory)
            api = QQBotAPI(settings)
            qq_vault, qq_forge = _build_tool_forge(plane, data_directory, qq_shared)
            ports = _resolved_ports(arguments.config)
            dss_url = arguments.dssuperagent_url or ports.dssuperagent_url
            workflow = _qq_workflow(plane, data_directory, arguments.config, dss_url,
                                    arguments.decision_timeout, settings, knowledge_service,
                                    tool_forge=qq_forge)
            service = QQChannelService(
                plane,
                workflow,
                api,
                settings,
                knowledge_service=knowledge_service,
                workspace_manager=getattr(workflow, "manager", None),
                attachment_ingestor=AttachmentIngestor(
                    plane.store, knowledge_service=knowledge_service,
                    storage_directory=data_directory / "attachments",
                ),
                tool_forge=qq_forge,
                forge_secrets_vault=qq_vault,
            )
            service.flush_outbox()
            outbox_stop = threading.Event()
            outbox_thread = threading.Thread(
                target=_run_outbox_retry,
                args=(service, outbox_stop, plane, getattr(workflow, "manager", None)),
                name="company-agent-outbox-retry",
                daemon=True,
            )
            outbox_thread.start()
            threading.Thread(target=service.resume_pending_attachments, name="company-agent-attachment-recovery",
                             daemon=True).start()
            gate_settings = WebGateSettings.load(arguments.config)
            if gate_settings.owner == "qq":
                active_web_gate = _start_web_gate(plane, arguments.config, gate_settings)
            if _configured_owner(arguments.config, "COMPANY_AGENT_KNOWLEDGE_API_OWNER", "qq") == "qq" and knowledge_service:
                active_query_server = _start_knowledge_server(knowledge_service)
            if _configured_owner(arguments.config, "COMPANY_AGENT_FORGE_MCP_OWNER", "admin") == "qq":
                active_forge_mcp = _start_forge_mcp(plane, arguments.config, data_directory, qq_shared)
            try:
                asyncio.run(QQGateway(api, service.process_prepared_event,
                                      on_intake=service.prepare_gateway_event,
                                      on_attachments=service.process_prepared_attachments,
                                      on_status=lambda state: plane.set_runtime_status(
                                          "qq_gateway", {"state": state, "updated_at": plane.now()})).run_forever())
            finally:
                service.close()
                outbox_stop.set()
                outbox_thread.join(timeout=2)
            return 0
        if arguments.command == "assistant-event":
            event_value = json.loads(arguments.event_file.read_text(encoding="utf-8-sig"))
            if not isinstance(event_value, dict):
                raise ValueError("event file must contain one JSON object")
            event = Event.from_dict(event_value)
            knowledge_service = _load_knowledge_service(plane, arguments.config, data_directory)
            gate_settings = WebGateSettings.load(arguments.config)
            if gate_settings.owner == "qq":
                active_web_gate = _start_web_gate(plane, arguments.config, gate_settings)
            if _configured_owner(arguments.config, "COMPANY_AGENT_KNOWLEDGE_API_OWNER", "qq") == "qq" and knowledge_service:
                active_query_server = _start_knowledge_server(knowledge_service)
            if knowledge_service and event.source_type.startswith("qq."):
                from dataclasses import replace
                event = replace(event, knowledge_grant=knowledge_service.issue_grant(event))
            policy = AssistantPolicy(auto_execute_capabilities=frozenset(arguments.auto_execute_capability))
            if policy.auto_execute_capabilities and not arguments.workspace:
                raise ValueError("--workspace is required when auto-execution capabilities are enabled")
            try:
                existing_run = plane.get_assistant_run(event.event_id)
            except NotFound:
                existing_run = None
            needs_dsh = bool(policy.auto_execute_capabilities) or bool(
                existing_run and existing_run.auto_authorized
                and existing_run.status not in {"observed", "proposed", "filtered", "completed", "execution_failed"}
            )
            manager = WorkspaceManager(data_directory / "workspaces") if needs_dsh else None
            dispatcher = None
            runtime_mcp_servers, runtime_mcp_secrets = _load_runtime_mcp(arguments.config, data_directory)
            mcp_provider = lambda: _effective_mcp_servers(plane, runtime_mcp_servers)
            ev_shared = SharedToolsManager(data_directory / "shared-tools")
            _, ev_forge = _build_tool_forge(plane, data_directory, ev_shared)
            if manager is not None:
                config = RuntimeConfig.load(arguments.config)
                adapter = DshAdapter(
                    config, manager, data_directory / "runtime",
                    web_search_enabled=lambda: bool(plane.get_setting("web_search_enabled", config.web_search_enabled)),
                    mcp_servers_provider=mcp_provider,
                    mcp_secrets_provider=lambda: runtime_mcp_secrets,
                )
                dispatcher = ExecutionDispatcher(plane, manager, adapter, data_directory / "runtime",
                                                 knowledge_service=knowledge_service)
            dss_url = arguments.dssuperagent_url or _resolved_ports(arguments.config).dssuperagent_url
            workflow = DssuperAgentWorkflow(
                plane,
                DssuperAgentAdapter(dss_url),
                manager=manager,
                dispatcher=dispatcher,
                knowledge_service=knowledge_service,
                shared_tools=ev_shared,
                tool_forge=ev_forge,
                mcp_servers_provider=mcp_provider,
                policy=policy,
                decision_timeout=arguments.decision_timeout,
            )
            run = workflow.process_event(event, workspace_id=arguments.workspace)
            print(json.dumps(run.to_dict(), ensure_ascii=False, indent=2))
            return 0 if run.status in {"observed", "proposed", "completed", "filtered"} else 1
        if arguments.command == "submit":
            manager = WorkspaceManager(data_directory / "workspaces")
            manager.provision(arguments.workspace)
            identifier = arguments.execution_id or f"execution-{uuid4().hex}"
            prompt = arguments.task_file.read_text(encoding="utf-8-sig")
            parameters = {"prompt": prompt, "expected_artifacts": arguments.artifact, "timeout_seconds": arguments.timeout}
            from .dsh import DshTask
            from .models import ExecutionRequest

            DshTask.from_request(ExecutionRequest(identifier, identifier, identifier, arguments.workspace,
                                                  "dsh.task", parameters, "queued", plane.now(), plane.now()))
            event_id, work_id, step_id = f"event-{identifier}", f"work-{identifier}", f"step-{identifier}"
            plane.ingest_event(Event(event_id, "local-dev", "console", "local-console", "local-operator",
                                     plane.now(), prompt, [], ["dsh.task"]))
            plane.create_work_item(work_item_id=work_id, title="Local development task", objective=prompt,
                                    source_event_ids=[event_id], completion_conditions=arguments.artifact or ["DSH completes"],
                                    risk_class="low", owner_ref="local-operator")
            plane.propose_work_item(work_id)
            plane.accept_work_item(work_id)
            plane.queue_work_item(work_id)
            plane.create_plan(plan_id=f"plan-{identifier}", work_item_id=work_id,
                               steps=[PlanStep(step_id, 1, "Execute task", capability="dsh.task")])
            request = plane.create_execution_request(execution_id=identifier, work_item_id=work_id,
                                                      plan_step_id=step_id, workspace_id=arguments.workspace,
                                                      capability="dsh.task", parameters=parameters)
            print(json.dumps({"execution_id": identifier, "status": request.status}, indent=2))
            return 0
        if arguments.command == "result":
            saved = plane.get_execution_result(arguments.execution_id)
        else:
            manager = WorkspaceManager(data_directory / "workspaces")
            config = RuntimeConfig.load(arguments.config)
            exec_mcp_servers, exec_mcp_secrets = _load_runtime_mcp(arguments.config, data_directory)
            adapter = DshAdapter(
                config, manager, data_directory / "runtime",
                web_search_enabled=lambda: bool(plane.get_setting("web_search_enabled", config.web_search_enabled)),
                mcp_servers_provider=lambda: _effective_mcp_servers(plane, exec_mcp_servers),
                mcp_secrets_provider=lambda: exec_mcp_secrets,
            )
            knowledge_service = _load_knowledge_service(plane, arguments.config, data_directory)
            saved = ExecutionDispatcher(plane, manager, adapter, data_directory / "runtime",
                                        knowledge_service=knowledge_service).dispatch(arguments.execution_id)
        print(json.dumps(saved.to_dict(), ensure_ascii=False, indent=2))
        return 0 if saved.status == "succeeded" else 1
    except (ValueError, OSError, ControlPlaneError, WorkspaceError, ExecutionBusy, DssuperAgentError,
            RuntimeError) as error:
        print(f"company-agent: {error}", file=sys.stderr)
        return 2
    finally:
        if active_forge_mcp:
            active_forge_mcp.close()
        if active_query_server:
            active_query_server.close()
        if active_web_gate:
            active_web_gate.close()
        if plane is not None:
            plane.close()


def _load_knowledge_service(plane: ControlPlane, config_path: Path, data_directory: Path) -> KnowledgeService | None:
    settings = KnowledgeSettings.load_optional(config_path)
    return KnowledgeService(plane.store, settings, data_directory / "knowledge-originals") if settings else None


def _initial_flag(config_path: Path, name: str) -> bool:
    values = RuntimeConfig._parse_env(config_path) if config_path.is_file() else {}
    return (values.get(name, "false").strip().lower() in {"1", "true", "yes", "on"})


def _ensure_default_settings(plane: ControlPlane) -> None:
    defaults: dict[str, Any] = {
        "ambient_listen_enabled": True,
        "ambient_cooldown_seconds": int(DEFAULT_AMBIENT_COOLDOWN_SECONDS),
        "bot_aliases": list(DEFAULT_BOT_ALIASES),
        "workspace_file_retention_days": DEFAULT_WORKSPACE_RETENTION_DAYS,
    }
    for key, value in defaults.items():
        if plane.get_setting(key, None) is None:
            plane.set_setting(key, value)
    if not plane.get_setting("ambient_cooldown_default_v2", False):
        current = plane.get_setting("ambient_cooldown_seconds", None)
        if current in (90, 90.0):
            plane.set_setting("ambient_cooldown_seconds", int(DEFAULT_AMBIENT_COOLDOWN_SECONDS))
        plane.set_setting("ambient_cooldown_default_v2", True)


def _resolved_ports(config_path: Path):
    values = RuntimeConfig._parse_env(config_path) if config_path.is_file() else {}
    return resolve_port_settings(values)


def _start_knowledge_server(knowledge_service: KnowledgeService) -> KnowledgeQueryServer:
    assert_port_available(
        knowledge_service.settings.query_api_host,
        knowledge_service.settings.query_api_port,
        service_name="Knowledge Query API",
        env_var="COMPANY_AGENT_KNOWLEDGE_API_PORT",
    )
    server = KnowledgeQueryServer(knowledge_service, knowledge_service.settings.query_api_port)
    server.start()
    return server


def _build_tool_forge(plane: ControlPlane, data_directory: Path,
                      shared_tools: SharedToolsManager | None = None) -> tuple[ForgeSecretsVault, ToolForgeService]:
    tools = shared_tools or SharedToolsManager(data_directory / "shared-tools")
    vault = ForgeSecretsVault(data_directory / "forge-secrets.json")
    return vault, ToolForgeService(plane, secrets_vault=vault, shared_tools=tools)


def _start_forge_mcp(plane: ControlPlane, config_path: Path, data_directory: Path,
                     shared_tools: SharedToolsManager) -> ToolForgeMcpServer | None:
    values = RuntimeConfig._parse_env(config_path) if config_path.is_file() else {}
    enabled, host, port, _ = parse_forge_mcp_settings(values)
    if not enabled:
        return None
    assert_port_available(host, port, service_name="Tool Forge MCP", env_var="COMPANY_AGENT_FORGE_MCP_PORT")
    tokens = ensure_forge_mcp_tokens(data_directory, values)
    vault = ForgeSecretsVault(data_directory / "forge-secrets.json")
    server = ToolForgeMcpServer(
        plane=plane,
        secrets_vault=vault,
        shared_tools=shared_tools,
        tokens=tokens,
        host=host,
        port=port,
    )
    server.start()
    return server


def _load_runtime_mcp(config_path: Path, data_directory: Path | None = None) -> tuple[tuple[McpServerSpec, ...], dict[str, str]]:
    values = RuntimeConfig._parse_env(config_path) if config_path.is_file() else {}
    servers = parse_mcp_servers_json(values.get("COMPANY_AGENT_MCP_SERVERS", ""))
    secrets = load_mcp_secrets(values, servers)
    enabled, forge_host, forge_port, _ = parse_forge_mcp_settings(values)
    if enabled and data_directory is not None:
        tokens = ensure_forge_mcp_tokens(data_directory, values)
        forge_specs = build_forge_mcp_specs(forge_host, forge_port)
        forge_secrets = forge_mcp_secrets_map(tokens)
        existing_ids = {s.server_id for s in servers}
        servers = servers + tuple(s for s in forge_specs if s.server_id not in existing_ids)
        for key, val in forge_secrets.items():
            secrets.setdefault(key, val)
    return servers, secrets


def _effective_mcp_servers(plane: ControlPlane, runtime_servers: tuple[McpServerSpec, ...]) -> tuple[McpServerSpec, ...]:
    raw_console = plane.get_setting("mcp_servers", [])
    return merge_mcp_servers(runtime_servers, raw_console if isinstance(raw_console, list) else [])


def _qq_workflow(plane: ControlPlane, data_directory: Path, config_path: Path, dssuperagent_url: str,
                 decision_timeout: float, qq_settings: QQBotSettings,
                 knowledge_service: KnowledgeService | None,
                 *, tool_forge: ToolForgeService | None = None) -> DssuperAgentWorkflow:
    manager = None
    dispatcher = None
    shared_tools = SharedToolsManager(data_directory / "shared-tools")
    forge = tool_forge or _build_tool_forge(plane, data_directory, shared_tools)[1]
    runtime_mcp_servers, _ = _load_runtime_mcp(config_path, data_directory)
    mcp_provider = lambda: _effective_mcp_servers(plane, runtime_mcp_servers)
    mcp_secrets_provider = lambda: _load_runtime_mcp(config_path, data_directory)[1]
    # Keep the execution path available whenever knowledge-backed work can be
    # delivered later, even when QQ auto-execution remains disabled. Manual
    # approval must not depend on the startup allowlist.
    if qq_settings.auto_execute_capabilities or knowledge_service is not None:
        runtime = RuntimeConfig.load(config_path)
        manager = WorkspaceManager(data_directory / "workspaces")
        adapter = DshAdapter(
            runtime, manager, data_directory / "runtime",
            web_search_enabled=lambda: bool(plane.get_setting("web_search_enabled", runtime.web_search_enabled)),
            mcp_servers_provider=mcp_provider,
            mcp_secrets_provider=mcp_secrets_provider,
        )
        dispatcher = ExecutionDispatcher(
            plane, manager, adapter, data_directory / "runtime", knowledge_service=knowledge_service,
        )
    policy = AssistantPolicy(auto_execute_capabilities=qq_settings.auto_execute_capabilities)
    return DssuperAgentWorkflow(
        plane, DssuperAgentAdapter(dssuperagent_url), manager=manager, dispatcher=dispatcher,
        knowledge_service=knowledge_service, shared_tools=shared_tools,
        tool_forge=forge,
        mcp_servers_provider=mcp_provider,
        policy=policy, decision_timeout=decision_timeout,
    )


def _run_outbox_retry(service: QQChannelService, stop: threading.Event,
                      plane: ControlPlane | None = None,
                      manager: WorkspaceManager | None = None,
                      interval_seconds: float = 15.0,
                      prune_interval_seconds: float = 21600.0) -> None:
    import time as _time
    last_prune: float | None = None
    while not stop.wait(interval_seconds):
        try:
            service.flush_outbox(respect_backoff=True)
        except Exception as error:
            print(f"company-agent: background QQ Outbox flush failed ({type(error).__name__})", file=sys.stderr)
        now = _time.monotonic()
        if manager is not None and plane is not None and (
            last_prune is None or (now - last_prune) >= prune_interval_seconds
        ):
            last_prune = now
            try:
                prune_expired_workspace_files(manager, retention_days=get_retention_days(plane))
            except Exception as error:
                print(f"company-agent: workspace retention prune failed ({type(error).__name__})", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
