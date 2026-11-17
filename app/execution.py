from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .control_plane import ControlPlane, InvalidTransition
from .dsh import DshAdapter
from .execution_state import private_directory, write_private_json
from .models import ExecutionRequest, ExecutionResult
from .runtime_config import POLICY_VERSION
from .workspace import WorkspaceManager, validate_execution_id
from .knowledge.delivery import KnowledgeDelivery
from .knowledge.service import KnowledgeService


TERMINAL_STATUSES = {"succeeded", "failed", "timed_out", "cancelled"}


class ExecutionDispatcher:
    """Authorize once, execute once, and reconcile receipts without replaying tools."""

    def __init__(self, plane: ControlPlane, manager: WorkspaceManager, adapter: DshAdapter,
                 runtime_directory: Path, *, knowledge_service: KnowledgeService | None = None) -> None:
        self.plane = plane
        self.manager = manager
        self.adapter = adapter
        self.knowledge_service = knowledge_service
        self.knowledge_delivery = KnowledgeDelivery(knowledge_service, manager) if knowledge_service else None
        self.receipt_directory = private_directory(runtime_directory.resolve() / "receipts")

    def dispatch(self, execution_id: str) -> ExecutionResult:
        validate_execution_id(execution_id)
        if len(execution_id) > 128:
            raise ValueError("execution_id exceeds 128 characters")
        request = self.plane.get_execution(execution_id)
        if request.capability != "dsh.task":
            raise ValueError("dispatcher only accepts dsh.task")
        with self.manager.execution_lease(request.workspace_id) as lease:
            request = self.plane.get_execution(execution_id)
            if request.status in TERMINAL_STATUSES:
                return self.plane.get_execution_result(execution_id)
            fingerprint = self._fingerprint(request)
            receipt = self.receipt_directory / f"{execution_id}.json"
            if receipt.is_symlink():
                raise ValueError("execution receipt cannot be a symbolic link")
            if receipt.exists():
                envelope = json.loads(receipt.read_text(encoding="utf-8"))
                if envelope.get("request_fingerprint") != fingerprint:
                    raise ValueError("execution receipt does not match the immutable request")
                result = ExecutionResult(**envelope["result"])
                if result.execution_id != execution_id or result.status not in TERMINAL_STATUSES:
                    raise ValueError("invalid execution receipt")
                self._finish(request, result)
                return result
            workspace = self.manager.get(request.workspace_id)
            if request.status == "running":
                result = ExecutionResult(
                    execution_id, "failed",
                    error="Execution was interrupted without a result receipt; inspect its effects before creating a new request",
                    metadata={"workspace_id": workspace.workspace_id, "error_category": "interrupted",
                              "finished_at": self.plane.now(), "policy_version": POLICY_VERSION},
                )
            elif request.status == "queued":
                if workspace.status != "ready":
                    raise InvalidTransition(f"workspace is {workspace.status}, not ready")
                task = self.adapter.preflight(request, workspace)
                request = self.plane.start_execution(execution_id)
                staged = []
                try:
                    self.manager.mark_running(workspace.workspace_id)
                    self.manager.save_execution_record(workspace.workspace_id, {
                        "execution_id": execution_id, "runtime": "dsh", "status": "running",
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "request_fingerprint": fingerprint,
                    })
                    if task.knowledge_document_ids or task.attachment_ids:
                        if self.knowledge_delivery is None:
                            raise ValueError("selected knowledge files cannot be delivered without the knowledge service")
                        work_item = self.plane.get_work_item(request.work_item_id)
                        if not work_item.source_event_ids:
                            raise ValueError("work item has no source event for knowledge authorization")
                        source_event = self.plane.get_event(work_item.source_event_ids[0])
                        staged = self.knowledge_delivery.stage(source_event, workspace, execution_id,
                                                               list(task.knowledge_document_ids),
                                                               list(task.attachment_ids))
                        references = "\n".join(f"- `{item.relative_path}` ({item.filename})" for item in staged)
                        task = replace(task, prompt=(task.prompt + "\n\nApproved reference files staged in this workspace:\n"
                                                     + references))
                    result = self.adapter.execute(request, workspace, task, lease_fd=lease.fileno())
                except Exception as error:
                    result = ExecutionResult(
                        execution_id, "failed", error=self.adapter.config.redact(f"Execution failed: {type(error).__name__}: {error}"),
                        metadata={"workspace_id": workspace.workspace_id, "error_category": "adapter_error",
                                  "finished_at": self.plane.now(), "policy_version": POLICY_VERSION},
                    )
                finally:
                    if staged and self.knowledge_delivery:
                        try:
                            self.knowledge_delivery.cleanup(workspace, execution_id)
                        except OSError:
                            pass
            else:
                raise InvalidTransition(f"cannot dispatch execution in {request.status}")
            # Persist outside the Agent's writable root before changing task state.
            write_private_json(receipt, {"request_fingerprint": fingerprint, "result": result.to_dict()})
            self._finish(request, result)
            return result

    def _finish(self, request: ExecutionRequest, result: ExecutionResult) -> None:
        workspace = self.manager.get(request.workspace_id)
        if workspace.status in {"ready", "running", "recovering"}:
            self.manager.save_execution_record(workspace.workspace_id, {
                "execution_id": result.execution_id, "runtime": "dsh", "status": result.status,
                "result": result.to_dict(),
            })
            if workspace.status in {"running", "recovering"}:
                self.manager.mark_ready(workspace.workspace_id)
        self.plane.record_execution_result(result)

    @staticmethod
    def _fingerprint(request: ExecutionRequest) -> str:
        value = request.to_dict()
        value.pop("status")
        value.pop("updated_at")
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
