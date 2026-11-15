from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from .assistant_workflow import AssistantPolicy, DssuperAgentWorkflow
from .control_plane import ControlPlane, ControlPlaneError, NotFound
from .dsh import DshAdapter
from .dssuperagent import DssuperAgentAdapter, DssuperAgentError
from .execution import ExecutionDispatcher
from .execution_state import ExecutionBusy, private_directory
from .models import Event, PlanStep
from .runtime_config import RuntimeConfig
from .workspace import WorkspaceError, WorkspaceManager


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
    assistant_event.add_argument("--decision-timeout", type=float, default=120)
    arguments = parser.parse_args()
    plane = None
    try:
        data_directory = private_directory(arguments.data_dir.expanduser().resolve())
        if arguments.command == "workspace":
            manager = WorkspaceManager(data_directory / "workspaces")
            manager.provision(arguments.workspace_id)
            print(json.dumps(manager.summary(arguments.workspace_id), ensure_ascii=False, indent=2))
            return 0
        plane = ControlPlane.open(data_directory / "control.sqlite")
        if arguments.command == "assistant-event":
            event_value = json.loads(arguments.event_file.read_text(encoding="utf-8-sig"))
            if not isinstance(event_value, dict):
                raise ValueError("event file must contain one JSON object")
            event = Event.from_dict(event_value)
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
                adapter = DshAdapter(config, manager, data_directory / "runtime")
                dispatcher = ExecutionDispatcher(plane, manager, adapter, data_directory / "runtime")
            workflow = DssuperAgentWorkflow(
                plane,
                DssuperAgentAdapter(arguments.dssuperagent_url),
                manager=manager,
                dispatcher=dispatcher,
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
            adapter = DshAdapter(config, manager, data_directory / "runtime")
            saved = ExecutionDispatcher(plane, manager, adapter, data_directory / "runtime").dispatch(arguments.execution_id)
        print(json.dumps(saved.to_dict(), ensure_ascii=False, indent=2))
        return 0 if saved.status == "succeeded" else 1
    except (ValueError, OSError, ControlPlaneError, WorkspaceError, ExecutionBusy, DssuperAgentError) as error:
        print(f"company-agent: {error}", file=sys.stderr)
        return 2
    finally:
        if plane is not None:
            plane.close()


if __name__ == "__main__":
    raise SystemExit(main())
