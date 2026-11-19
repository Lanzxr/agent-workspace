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
from .models import Event, PlanStep
from .qq_channel import QQBotAPI, QQChannelService, QQGateway
from .runtime_config import QQBotSettings, RuntimeConfig
from .scheduler import ScheduleService, SchedulerWorker
from .scheduled_resume import ScheduledResumeHandler
from .workspace import WorkspaceError, WorkspaceManager
from .web_gate import WebGateServer, WebGateSettings


def _start_web_gate(plane, config: Path, settings: WebGateSettings) -> WebGateServer:
    server = WebGateServer(
        settings,
        enabled=lambda: bool(plane.get_setting("dssuperagent_web_search_enabled",
                                                _initial_flag(config, "AGENT_DSSUPERAGENT_WEB_SEARCH_ENABLED"))),
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
    submit.add_argument("--timeout", type=float, default=240)
    submit.add_argument("--execution-id", default=None)
    execute = commands.add_parser("execute", help="Dispatch or reconcile an existing Execution Request")
    execute.add_argument("execution_id")
    execute.add_argument("--config", type=Path, default=Path.home() / ".config/company-agent/runtime.env")
    result = commands.add_parser("result", help="Read a persisted result without calling the model")
    result.add_argument("execution_id")
    assistant_event = commands.add_parser("assistant-event", help="Send a standard event through DSSuperAgent")
    assistant_event.add_argument("--event-file", type=Path, required=True)
    assistant_event.add_argument("--dssuperagent-url", default="http://127.0.0.1:3082")
    assistant_event.add_argument("--workspace")
    assistant_event.add_argument("--auto-execute-capability", action="append", default=[])
    assistant_event.add_argument("--config", type=Path, default=Path.home() / ".config/company-agent/runtime.env")
    assistant_event.add_argument("--decision-timeout", type=float, default=DEFAULT_DECISION_TIMEOUT_SECONDS)
    qq_serve = commands.add_parser("qq-serve", help="Receive QQ group events through Company Agent")
    qq_serve.add_argument("--config", type=Path, default=Path.home() / ".config/company-agent/runtime.env")
    qq_serve.add_argument("--dssuperagent-url", default="http://127.0.0.1:3082")
    qq_serve.add_argument("--decision-timeout", type=float, default=DEFAULT_DECISION_TIMEOUT_SECONDS)
    admin = commands.add_parser("admin-serve", help="Run the authenticated local control console")
    admin.add_argument("--host", default="127.0.0.1")
    admin.add_argument("--port", type=int, default=8765)
    admin.add_argument("--config", type=Path, default=Path.home() / ".config/company-agent/runtime.env")
    admin.add_argument("--dssuperagent-url", default="http://127.0.0.1:3082")
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
            settings = QQBotSettings.load(arguments.config)
            knowledge_service = _load_knowledge_service(plane, arguments.config, data_directory)
            api = QQBotAPI(settings)
            scheduler_plane = ControlPlane.open(data_directory / "control.sqlite")
            workflow = _qq_workflow(scheduler_plane, data_directory, arguments.config,
                                    arguments.dssuperagent_url, arguments.decision_timeout,
                                    settings, knowledge_service)
            channel = QQChannelService(
                scheduler_plane,
                workflow,
                api,
                settings,
                knowledge_service=knowledge_service,
            )
            worker = SchedulerWorker(ScheduleService(scheduler_plane.store), ScheduledResumeHandler(channel))
            channel.flush_outbox()
            gate_settings = WebGateSettings.load(arguments.config)
            if gate_settings.owner == "admin":
                active_web_gate = _start_web_gate(plane, arguments.config, gate_settings)
            if _configured_owner(arguments.config, "COMPANY_AGENT_KNOWLEDGE_API_OWNER", "qq") == "admin" and knowledge_service:
                active_query_server = KnowledgeQueryServer(knowledge_service, knowledge_service.settings.query_api_port)
                active_query_server.start()
            serve_admin(plane, data_directory, arguments.host, arguments.port, token, worker,
                        knowledge_service=knowledge_service)
            return 0
        if arguments.command == "outbox":
            if arguments.outbox_command == "list":
                print(json.dumps([message.to_dict() for message in plane.list_outbox()], ensure_ascii=False, indent=2))
                return 0
            message = plane.resolve_outbox_delivery(arguments.outbox_id, delivered=arguments.delivered)
            print(json.dumps(message.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if arguments.command == "qq-serve":
            settings = QQBotSettings.load(arguments.config)
            knowledge_service = _load_knowledge_service(plane, arguments.config, data_directory)
            api = QQBotAPI(settings)
            service = QQChannelService(
                plane,
                _qq_workflow(plane, data_directory, arguments.config, arguments.dssuperagent_url,
                             arguments.decision_timeout, settings, knowledge_service),
                api,
                settings,
                knowledge_service=knowledge_service,
                attachment_ingestor=AttachmentIngestor(
                    plane.store, knowledge_service=knowledge_service,
                    storage_directory=data_directory / "attachments",
                ),
            )
            service.flush_outbox()
            threading.Thread(target=service.resume_pending_attachments, name="company-agent-attachment-recovery",
                             daemon=True).start()
            gate_settings = WebGateSettings.load(arguments.config)
            if gate_settings.owner == "qq":
                active_web_gate = _start_web_gate(plane, arguments.config, gate_settings)
            if _configured_owner(arguments.config, "COMPANY_AGENT_KNOWLEDGE_API_OWNER", "qq") == "qq" and knowledge_service:
                active_query_server = KnowledgeQueryServer(knowledge_service, knowledge_service.settings.query_api_port)
                active_query_server.start()
            asyncio.run(QQGateway(api, service.process_prepared_event,
                                  on_intake=service.prepare_gateway_event,
                                  on_attachments=service.process_prepared_attachments,
                                  on_status=lambda state: plane.set_runtime_status(
                                      "qq_gateway", {"state": state, "updated_at": plane.now()})).run_forever())
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
                active_query_server = KnowledgeQueryServer(knowledge_service, knowledge_service.settings.query_api_port)
                active_query_server.start()
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
            if manager is not None:
                config = RuntimeConfig.load(arguments.config)
                adapter = DshAdapter(config, manager, data_directory / "runtime",
                                     web_search_enabled=lambda: bool(plane.get_setting("web_search_enabled", config.web_search_enabled)))
                dispatcher = ExecutionDispatcher(plane, manager, adapter, data_directory / "runtime",
                                                 knowledge_service=knowledge_service)
            workflow = DssuperAgentWorkflow(
                plane,
                DssuperAgentAdapter(arguments.dssuperagent_url),
                manager=manager,
                dispatcher=dispatcher,
                knowledge_service=knowledge_service,
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
            adapter = DshAdapter(config, manager, data_directory / "runtime",
                                 web_search_enabled=lambda: bool(plane.get_setting("web_search_enabled", config.web_search_enabled)))
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


def _qq_workflow(plane: ControlPlane, data_directory: Path, config_path: Path, dssuperagent_url: str,
                 decision_timeout: float, qq_settings: QQBotSettings,
                 knowledge_service: KnowledgeService | None) -> DssuperAgentWorkflow:
    manager = None
    dispatcher = None
    # Keep the execution path available whenever knowledge-backed work can be
    # delivered later, even when QQ auto-execution remains disabled. Manual
    # approval must not depend on the startup allowlist.
    if qq_settings.auto_execute_capabilities or knowledge_service is not None:
        runtime = RuntimeConfig.load(config_path)
        manager = WorkspaceManager(data_directory / "workspaces")
        adapter = DshAdapter(
            runtime, manager, data_directory / "runtime",
            web_search_enabled=lambda: bool(plane.get_setting("web_search_enabled", runtime.web_search_enabled)),
        )
        dispatcher = ExecutionDispatcher(
            plane, manager, adapter, data_directory / "runtime", knowledge_service=knowledge_service,
        )
    policy = AssistantPolicy(auto_execute_capabilities=qq_settings.auto_execute_capabilities)
    return DssuperAgentWorkflow(
        plane, DssuperAgentAdapter(dssuperagent_url), manager=manager, dispatcher=dispatcher,
        knowledge_service=knowledge_service, policy=policy, decision_timeout=decision_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
