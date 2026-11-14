from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from .control_plane import ControlPlane, ControlPlaneError
from .dsh import DshAdapter
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
    arguments = parser.parse_args()
    plane = None
    try:
        data_directory = private_directory(arguments.data_dir.expanduser().resolve())
        manager = WorkspaceManager(data_directory / "workspaces")
        if arguments.command == "workspace":
            manager.provision(arguments.workspace_id)
            print(json.dumps(manager.summary(arguments.workspace_id), ensure_ascii=False, indent=2))
            return 0
        plane = ControlPlane.open(data_directory / "control.sqlite")
        if arguments.command == "submit":
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
            config = RuntimeConfig.load(arguments.config)
            adapter = DshAdapter(config, manager, data_directory / "runtime")
            saved = ExecutionDispatcher(plane, manager, adapter, data_directory / "runtime").dispatch(arguments.execution_id)
        print(json.dumps(saved.to_dict(), ensure_ascii=False, indent=2))
        return 0 if saved.status == "succeeded" else 1
    except (ValueError, OSError, ControlPlaneError, WorkspaceError, ExecutionBusy) as error:
        print(f"company-agent: {error}", file=sys.stderr)
        return 2
    finally:
        if plane is not None:
            plane.close()


if __name__ == "__main__":
    raise SystemExit(main())
