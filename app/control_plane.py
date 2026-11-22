from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    Approval,
    AssistantRun,
    AuditEntry,
    Conversation,
    Event,
    ExecutionRequest,
    ExecutionResult,
    Memory,
    OutboxMessage,
    PlanStep,
    PublicationDecision,
    Thread,
    WorkItem,
    WorkPlan,
)
from .store import Store


class ControlPlaneError(Exception):
    """Base error for control-plane operations."""


class InvalidTransition(ControlPlaneError):
    """Raised when a work item or execution changes state illegally."""


class NotFound(ControlPlaneError):
    """Raised when a referenced control-plane record does not exist."""


WORK_ITEM_TRANSITIONS: dict[str, set[str]] = {
    "candidate": {"proposed"},
    "proposed": {"accepted", "rejected", "needs_approval"},
    "needs_approval": {"accepted", "rejected", "cancelled"},
    "accepted": {"queued", "blocked", "cancelled"},
    "queued": {"running", "failed", "blocked", "cancelled"},
    "running": {"completed", "failed", "blocked", "cancelled"},
    "blocked": {"accepted", "queued", "cancelled"},
    "failed": {"accepted", "queued", "cancelled"},
    "completed": set(),
    "rejected": set(),
    "cancelled": set(),
}


class ControlPlane:
    def __init__(self, store: Store) -> None:
        self.store = store

    @classmethod
    def open(cls, path: Path) -> "ControlPlane":
        plane = cls(Store(path))
        plane.recover_unfinished()
        return plane

    def close(self) -> None:
        self.store.close()

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.store.fetchone("SELECT payload FROM settings WHERE setting_key=?", (key,))
        return self.store.decode(row[0])["value"] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        if not key.strip() or len(key) > 120:
            raise ValueError("setting key must contain 1 to 120 characters")
        now = self.now()
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO settings(setting_key,payload,updated_at) VALUES (?,?,?) "
                               "ON CONFLICT(setting_key) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                               (key, self.store.encode({"value": value}), now))
            self._audit(connection, "setting", key, "updated", {"value": value}, now)

    def record_external_audit(self, action: str, payload: dict[str, Any]) -> None:
        """Record a bounded audit event emitted by an authenticated capability bridge."""
        now = self.now()
        with self.store.transaction() as connection:
            self._audit(connection, "capability", "web", action, payload, now)

    def set_runtime_status(self, key: str, value: Any) -> None:
        now = self.now()
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO settings(setting_key,payload,updated_at) VALUES (?,?,?) "
                               "ON CONFLICT(setting_key) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                               (key, self.store.encode({"value": value}), now))

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}-{uuid4()}"

    def ingest_event(self, event: Event) -> bool:
        created_at = self.now()
        with self.store.transaction() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO events(event_id, payload, created_at) VALUES (?, ?, ?)",
                (event.event_id, self.store.encode(event.to_dict()), created_at),
            ).rowcount
            if inserted == 0:
                return False
            self._audit(connection, "event", event.event_id, "ingested", {}, created_at)
        return True

    def get_event(self, event_id: str) -> Event:
        row = self.store.connection.execute(
            "SELECT payload FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"event not found: {event_id}")
        return Event.from_dict(self.store.decode(row[0]))

    def update_event(self, event: Event) -> Event:
        current = self.get_event(event.event_id)
        immutable = ("source_type", "source_id", "conversation_id", "actor_id", "occurred_at", "content",
                     "capabilities", "external_actor_id")
        if any(getattr(current, name) != getattr(event, name) for name in immutable):
            raise ControlPlaneError("event updates may only change attachment processing metadata")
        if current.attachments == event.attachments:
            return current
        now = self.now()
        with self.store.transaction() as connection:
            connection.execute("UPDATE events SET payload=? WHERE event_id=?",
                               (self.store.encode(event.to_dict()), event.event_id))
            self._audit(connection, "event", event.event_id, "attachments_updated",
                        {"attachment_count": len(event.attachments)}, now)
        return event

    def redact_event_content(self, event_id: str, redacted_content: str) -> None:
        """Overwrite stored event text cleanly when a DM command carried a secret literal."""
        current = self.get_event(event_id)
        if current.content == redacted_content:
            return
        updated = Event(
            current.event_id, current.source_type, current.source_id,
            current.conversation_id, current.actor_id, current.occurred_at,
            redacted_content, current.attachments, current.capabilities,
            external_actor_id=current.external_actor_id,
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE events SET payload=? WHERE event_id=?",
                (self.store.encode(updated.to_dict()), event_id),
            )
            self._audit(connection, "event", event_id, "secret_redacted", {}, self.now())

    def create_conversation(
        self,
        *,
        conversation_id: str,
        kind: str,
        participant_refs: list[str],
        policy_version: str,
    ) -> Conversation:
        if not conversation_id.strip() or not kind.strip() or not policy_version.strip():
            raise ValueError("conversation identity and policy are required")
        now = self.now()
        conversation = Conversation(conversation_id, kind, list(participant_refs), policy_version, now, now)
        with self.store.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?", (conversation_id,)
            ).fetchone():
                raise ControlPlaneError(f"conversation already exists: {conversation_id}")
            connection.execute(
                "INSERT INTO conversations(conversation_id, payload, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conversation_id, self.store.encode(conversation.to_dict()), now, now),
            )
            self._audit(connection, "conversation", conversation_id, "created", {}, now)
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation:
        row = self.store.connection.execute(
            "SELECT payload FROM conversations WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"conversation not found: {conversation_id}")
        return Conversation.from_dict(self.store.decode(row[0]))

    def create_thread(
        self, *, thread_id: str, conversation_id: str, title: str
    ) -> Thread:
        self.get_conversation(conversation_id)
        if not thread_id.strip() or not title.strip():
            raise ValueError("thread identity and title are required")
        now = self.now()
        thread = Thread(thread_id, conversation_id, title, [], "open", now, now)
        with self.store.transaction() as connection:
            if connection.execute("SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)).fetchone():
                raise ControlPlaneError(f"thread already exists: {thread_id}")
            connection.execute(
                "INSERT INTO threads(thread_id, conversation_id, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (thread_id, conversation_id, self.store.encode(thread.to_dict()), now, now),
            )
            self._audit(connection, "thread", thread_id, "created", {}, now)
        return thread

    def attach_event_to_thread(self, thread_id: str, event_id: str) -> Thread:
        thread = self.get_thread(thread_id)
        self.get_event(event_id)
        if event_id in thread.event_ids:
            return thread
        thread.event_ids.append(event_id)
        thread.updated_at = self.now()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE threads SET payload = ?, updated_at = ? WHERE thread_id = ?",
                (self.store.encode(thread.to_dict()), thread.updated_at, thread_id),
            )
            self._audit(connection, "thread", thread_id, "event_attached", {"event_id": event_id}, thread.updated_at)
        return thread

    def get_thread(self, thread_id: str) -> Thread:
        row = self.store.connection.execute(
            "SELECT payload FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"thread not found: {thread_id}")
        return Thread.from_dict(self.store.decode(row[0]))

    def save_assistant_run(self, run: AssistantRun) -> AssistantRun:
        if not run.event_id.strip() or not run.status.strip() or not run.policy_version.strip():
            raise ValueError("assistant run event_id, status and policy_version are required")
        self.get_event(run.event_id)
        now = self.now()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM assistant_runs WHERE event_id = ?", (run.event_id,)
            ).fetchone()
            if row is not None:
                previous = AssistantRun.from_dict(self.store.decode(row[0]))
                if previous.work_item_id and previous.work_item_id != run.work_item_id:
                    raise ControlPlaneError("assistant run cannot change its work item")
                if previous.policy_version != run.policy_version:
                    raise ControlPlaneError("assistant run cannot change its policy version")
                run.created_at = previous.created_at
            elif not run.created_at:
                run.created_at = now
            run.updated_at = now
            connection.execute(
                "INSERT INTO assistant_runs(event_id, work_item_id, payload, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(event_id) DO UPDATE SET "
                "work_item_id = excluded.work_item_id, payload = excluded.payload, "
                "status = excluded.status, updated_at = excluded.updated_at",
                (run.event_id, run.work_item_id, self.store.encode(run.to_dict()), run.status, run.created_at, now),
            )
            self._audit(connection, "assistant_run", run.event_id, f"status:{run.status}", {
                "event_id": run.event_id,
                "policy_version": run.policy_version,
                "decision_id": run.decision_id,
                "upstream_trigger_id": run.upstream_trigger_id,
                "work_item_id": run.work_item_id,
                "plan_id": run.plan_id,
                "execution_ids": list(run.execution_ids),
                "auto_authorized": run.auto_authorized,
                "workspace_id": run.workspace_id,
                "error_category": run.error_category,
            }, now)
        return run

    def get_assistant_run(self, event_id: str) -> AssistantRun:
        row = self.store.connection.execute(
            "SELECT payload FROM assistant_runs WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"assistant run not found: {event_id}")
        return AssistantRun.from_dict(self.store.decode(row[0]))

    def get_assistant_run_for_work_item(self, work_item_id: str) -> AssistantRun:
        row = self.store.connection.execute(
            "SELECT payload FROM assistant_runs WHERE work_item_id = ? ORDER BY created_at DESC LIMIT 1",
            (work_item_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"assistant run not found for work item: {work_item_id}")
        return AssistantRun.from_dict(self.store.decode(row[0]))

    def save_memory(
        self,
        *,
        memory_id: str,
        key: str,
        value: str,
        source_event_ids: list[str],
        confidence: float,
    ) -> Memory:
        if not key.strip() or not value.strip() or not 0 <= confidence <= 1:
            raise ValueError("memory key, value and confidence are invalid")
        for event_id in source_event_ids:
            self.get_event(event_id)
        now = self.now()
        memory = Memory(memory_id, key, value, list(source_event_ids), confidence, now, now)
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO memories(memory_id, memory_key, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(memory_id) DO UPDATE SET memory_key = excluded.memory_key, payload = excluded.payload, updated_at = excluded.updated_at",
                (memory_id, key, self.store.encode(memory.to_dict()), now, now),
            )
            self._audit(connection, "memory", memory_id, "saved", {}, now)
        return memory

    def get_memory(self, memory_id: str) -> Memory:
        row = self.store.connection.execute(
            "SELECT payload FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"memory not found: {memory_id}")
        return Memory.from_dict(self.store.decode(row[0]))

    def create_work_item(
        self,
        *,
        work_item_id: str,
        title: str,
        objective: str,
        source_event_ids: list[str],
        completion_conditions: list[str],
        risk_class: str,
        owner_ref: str | None,
    ) -> WorkItem:
        if not source_event_ids:
            raise ValueError("source_event_ids cannot be empty")
        if not title.strip() or not objective.strip() or not risk_class.strip() or not completion_conditions:
            raise ValueError("work item required fields are incomplete")
        for event_id in source_event_ids:
            self.get_event(event_id)
        now = self.now()
        item = WorkItem(
            work_item_id=work_item_id,
            title=title,
            objective=objective,
            status="candidate",
            source_event_ids=list(source_event_ids),
            completion_conditions=list(completion_conditions),
            risk_class=risk_class,
            owner_ref=owner_ref,
            created_at=now,
            updated_at=now,
        )
        with self.store.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM work_items WHERE work_item_id = ?", (work_item_id,)
            ).fetchone():
                raise ControlPlaneError(f"work item already exists: {work_item_id}")
            self._save_work_item(connection, item)
            self._audit(connection, "work_item", work_item_id, "created", {}, now)
        return item

    def get_work_item(self, work_item_id: str) -> WorkItem:
        row = self.store.connection.execute(
            "SELECT payload FROM work_items WHERE work_item_id = ?", (work_item_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"work item not found: {work_item_id}")
        return WorkItem.from_dict(self.store.decode(row[0]))

    def accept_work_item(self, work_item_id: str) -> WorkItem:
        return self._transition_work_item(work_item_id, "accepted")

    def propose_work_item(self, work_item_id: str) -> WorkItem:
        return self._transition_work_item(work_item_id, "proposed")

    def reject_work_item(self, work_item_id: str, reason: str) -> WorkItem:
        return self._transition_work_item(work_item_id, "rejected", {"reason": reason})

    def request_work_item_approval(self, work_item_id: str, reason: str) -> WorkItem:
        return self._transition_work_item(work_item_id, "needs_approval", {"reason": reason})

    def request_approval(
        self, *, approval_id: str, work_item_id: str, scope: str, policy_version: str
    ) -> Approval:
        item = self.get_work_item(work_item_id)
        if item.status != "needs_approval":
            raise InvalidTransition(f"work item is {item.status}, not needs_approval")
        if not scope.strip() or not policy_version.strip():
            raise ValueError("approval scope and policy_version are required")
        now = self.now()
        approval = Approval(approval_id, work_item_id, scope, policy_version, "requested", now)
        with self.store.transaction() as connection:
            if connection.execute("SELECT 1 FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone():
                raise ControlPlaneError(f"approval already exists: {approval_id}")
            connection.execute(
                "INSERT INTO approvals(approval_id, work_item_id, payload, status, requested_at) VALUES (?, ?, ?, ?, ?)",
                (approval_id, work_item_id, self.store.encode(approval.to_dict()), approval.status, now),
            )
            self._audit(connection, "approval", approval_id, "requested", {}, now)
        return approval

    def approve(self, approval_id: str, actor_ref: str) -> Approval:
        approval = self.get_approval(approval_id)
        if approval.status != "requested":
            raise InvalidTransition(f"approval is already {approval.status}")
        if not actor_ref.strip():
            raise ValueError("approval actor is required")
        approval.status = "approved"
        approval.actor_ref = actor_ref
        approval.decided_at = self.now()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE approvals SET payload = ?, status = ? WHERE approval_id = ?",
                (self.store.encode(approval.to_dict()), approval.status, approval_id),
            )
            self._audit(connection, "approval", approval_id, "approved", {"actor_ref": actor_ref}, approval.decided_at)
        return approval

    def get_approval(self, approval_id: str) -> Approval:
        row = self.store.connection.execute(
            "SELECT payload FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"approval not found: {approval_id}")
        return Approval.from_dict(self.store.decode(row[0]))

    def queue_work_item(self, work_item_id: str) -> WorkItem:
        return self._transition_work_item(work_item_id, "queued")

    def start_work_item(self, work_item_id: str) -> WorkItem:
        try:
            self._find_plan_for_work_item(work_item_id)
        except NotFound as error:
            raise InvalidTransition("work item cannot start without a plan") from error
        return self._transition_work_item(work_item_id, "running")

    def complete_work_item(self, work_item_id: str) -> WorkItem:
        try:
            plan = self._find_plan_for_work_item(work_item_id)
        except NotFound as error:
            raise InvalidTransition("work item cannot complete without a plan") from error
        if not plan.steps or any(step.status != "completed" for step in plan.steps):
            raise InvalidTransition("work item cannot complete before all plan steps complete")
        return self._transition_work_item(work_item_id, "completed")

    def block_work_item(self, work_item_id: str, reason: str) -> WorkItem:
        return self._transition_work_item(work_item_id, "blocked", {"reason": reason})

    def _transition_work_item(
        self, work_item_id: str, target: str, payload: dict[str, Any] | None = None
    ) -> WorkItem:
        item = self.get_work_item(work_item_id)
        if target not in WORK_ITEM_TRANSITIONS[item.status]:
            raise InvalidTransition(f"{item.status} -> {target} is not allowed")
        now = self.now()
        item.status = target
        item.updated_at = now
        with self.store.transaction() as connection:
            self._save_work_item(connection, item)
            self._audit(connection, "work_item", work_item_id, f"status:{target}", payload or {}, now)
        return item

    def create_plan(self, *, plan_id: str, work_item_id: str, steps: list[PlanStep]) -> WorkPlan:
        item = self.get_work_item(work_item_id)
        if item.status not in {"accepted", "queued", "running"}:
            raise InvalidTransition(f"cannot plan work item in {item.status}")
        if not steps:
            raise ValueError("steps cannot be empty")
        if len({step.step_id for step in steps}) != len(steps):
            raise ValueError("step ids must be unique")
        now = self.now()
        plan = WorkPlan(plan_id, work_item_id, list(steps), now, now)
        with self.store.transaction() as connection:
            if connection.execute("SELECT 1 FROM plans WHERE plan_id = ?", (plan_id,)).fetchone():
                raise ControlPlaneError(f"plan already exists: {plan_id}")
            connection.execute(
                "INSERT INTO plans(plan_id, work_item_id, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (plan_id, work_item_id, self.store.encode(plan.to_dict()), now, now),
            )
            self._audit(connection, "plan", plan_id, "created", {"work_item_id": work_item_id}, now)
        return plan

    def get_plan(self, plan_id: str) -> WorkPlan:
        row = self.store.connection.execute(
            "SELECT payload FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"plan not found: {plan_id}")
        return WorkPlan.from_dict(self.store.decode(row[0]))

    def create_execution_request(
        self,
        *,
        execution_id: str,
        work_item_id: str,
        plan_step_id: str,
        workspace_id: str,
        capability: str,
        parameters: dict[str, Any],
        approval_ref: str | None = None,
    ) -> ExecutionRequest:
        item = self.get_work_item(work_item_id)
        if item.status not in {"queued", "running"}:
            raise InvalidTransition(f"cannot execute work item in {item.status}")
        if not workspace_id.strip() or not capability.strip():
            raise ValueError("workspace_id and capability are required")
        if item.risk_class != "low":
            if not approval_ref:
                raise InvalidTransition("high-risk execution requires approval_ref")
            try:
                approval = self.get_approval(approval_ref)
            except NotFound as error:
                raise InvalidTransition("approval does not exist") from error
            if (approval.status != "approved" or approval.work_item_id != work_item_id
                    or approval.scope != capability):
                raise InvalidTransition("approval is not approved for this work item")
        plan = self._find_plan_for_work_item(work_item_id)
        step = next((candidate for candidate in plan.steps if candidate.step_id == plan_step_id), None)
        if step is None:
            raise NotFound(f"plan step not found: {plan_step_id}")
        if step.status != "pending":
            raise InvalidTransition(f"plan step is already {step.status}")
        if step.capability and step.capability != capability:
            raise InvalidTransition("execution capability does not match plan step")
        step.status = "ready"
        plan.updated_at = self.now()
        now = plan.updated_at
        request = ExecutionRequest(
            execution_id=execution_id,
            work_item_id=work_item_id,
            plan_step_id=plan_step_id,
            workspace_id=workspace_id,
            capability=capability,
            parameters=dict(parameters),
            status="queued",
            created_at=now,
            updated_at=now,
            approval_ref=approval_ref,
        )
        with self.store.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM executions WHERE execution_id = ?", (execution_id,)
            ).fetchone():
                raise ControlPlaneError(f"execution already exists: {execution_id}")
            self._save_plan(connection, plan)
            connection.execute(
                "INSERT INTO executions(execution_id, work_item_id, payload, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (execution_id, work_item_id, self.store.encode(request.to_dict()), request.status, now, now),
            )
            self._audit(connection, "execution", execution_id, "queued", {"capability": capability}, now)
        return request

    def get_execution(self, execution_id: str) -> ExecutionRequest:
        row = self.store.connection.execute(
            "SELECT payload FROM executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"execution not found: {execution_id}")
        return ExecutionRequest.from_dict(self.store.decode(row[0]))

    def get_execution_result(self, execution_id: str) -> ExecutionResult:
        row = self.store.connection.execute(
            "SELECT payload FROM execution_results WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"execution result not found: {execution_id}")
        return ExecutionResult(**self.store.decode(row[0]))

    def start_execution(self, execution_id: str) -> ExecutionRequest:
        with self.store.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._start_execution(connection, execution_id)

    def _start_execution(self, connection: sqlite3.Connection, execution_id: str) -> ExecutionRequest:
        request = self.get_execution(execution_id)
        if request.status != "queued":
            raise InvalidTransition(f"execution is already {request.status}")
        request.status = "running"
        request.updated_at = self.now()
        plan = self._find_plan_for_work_item(request.work_item_id)
        step = next(step for step in plan.steps if step.step_id == request.plan_step_id)
        step.status = "running"
        plan.updated_at = request.updated_at
        item = self.get_work_item(request.work_item_id)
        if item.status not in {"queued", "running"}:
            raise InvalidTransition(f"cannot execute work item in {item.status}")
        if item.risk_class != "low":
            if not request.approval_ref:
                raise InvalidTransition("execution requires approval_ref")
            approval = self.get_approval(request.approval_ref)
            if (approval.status != "approved" or approval.work_item_id != item.work_item_id
                    or approval.scope != request.capability):
                raise InvalidTransition("approval does not authorize this capability")
        if item.status == "queued":
            item.status = "running"
            item.updated_at = request.updated_at
        connection.execute(
            "UPDATE executions SET payload = ?, status = ?, updated_at = ? WHERE execution_id = ?",
            (self.store.encode(request.to_dict()), request.status, request.updated_at, execution_id),
        )
        self._save_plan(connection, plan)
        self._save_work_item(connection, item)
        self._audit(connection, "execution", execution_id, "started", {}, request.updated_at)
        return request

    def record_execution_result(self, result: ExecutionResult) -> ExecutionRequest:
        with self.store.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._record_execution_result(connection, result)

    def fail_queued_execution(
        self,
        execution_id: str,
        *,
        error: str,
        error_category: str = "execution_failed",
        result_status: str = "failed",
    ) -> ExecutionRequest | None:
        """Settle a queued execution that failed before DSH launch so it never stays queued forever."""
        if result_status not in {"failed", "timed_out", "cancelled"}:
            raise ValueError(f"unsupported execution result: {result_status}")
        with self.store.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                request = self.get_execution(execution_id)
            except NotFound:
                return None
            if request.status != "queued":
                return request
            now = self.now()
            request.status = result_status
            request.updated_at = now
            plan = self._find_plan_for_work_item(request.work_item_id)
            step = next(step for step in plan.steps if step.step_id == request.plan_step_id)
            step.status = "failed"
            plan.updated_at = now
            item = self.get_work_item(request.work_item_id)
            if item.status in {"queued", "running"}:
                item.status = "failed"
                item.updated_at = now
            result = ExecutionResult(
                execution_id=execution_id,
                status=result_status,
                error=error[:1000],
                metadata={
                    "workspace_id": request.workspace_id,
                    "error_category": error_category,
                    "finished_at": now,
                    "pre_start": True,
                },
            )
            connection.execute(
                "UPDATE executions SET payload = ?, status = ?, updated_at = ? WHERE execution_id = ?",
                (self.store.encode(request.to_dict()), request.status, now, execution_id),
            )
            connection.execute(
                "INSERT OR REPLACE INTO execution_results(execution_id, payload, created_at) VALUES (?, ?, ?)",
                (execution_id, self.store.encode(result.to_dict()), now),
            )
            self._save_plan(connection, plan)
            self._save_work_item(connection, item)
            self._audit(
                connection,
                "execution",
                execution_id,
                f"result:{result_status}",
                {"error_category": error_category, "pre_start": True},
                now,
            )
            return request

    def _record_execution_result(self, connection: sqlite3.Connection, result: ExecutionResult) -> ExecutionRequest:
        request = self.get_execution(result.execution_id)
        if request.status != "running":
            raise InvalidTransition(f"execution is {request.status}")
        if result.status not in {"succeeded", "failed", "timed_out", "cancelled"}:
            raise ValueError(f"unsupported execution result: {result.status}")
        now = self.now()
        request.status = result.status
        request.updated_at = now
        plan = self._find_plan_for_work_item(request.work_item_id)
        step = next(step for step in plan.steps if step.step_id == request.plan_step_id)
        step.status = "completed" if result.status == "succeeded" else "failed"
        plan.updated_at = now
        item = self.get_work_item(request.work_item_id)
        if (item.status == "running" and result.status == "succeeded"
                and all(candidate.status == "completed" for candidate in plan.steps)):
            item.status = "completed"
            item.updated_at = now
        elif item.status == "running" and result.status != "succeeded":
            item.status = "failed"
            item.updated_at = now
        connection.execute(
            "UPDATE executions SET payload = ?, status = ?, updated_at = ? WHERE execution_id = ?",
            (self.store.encode(request.to_dict()), request.status, now, request.execution_id),
        )
        connection.execute(
            "INSERT OR REPLACE INTO execution_results(execution_id, payload, created_at) VALUES (?, ?, ?)",
            (result.execution_id, self.store.encode(result.to_dict()), now),
        )
        self._save_plan(connection, plan)
        self._save_work_item(connection, item)
        self._audit(connection, "execution", result.execution_id, f"result:{result.status}", {}, now)
        return request

    def decide_publication(
        self,
        *,
        decision_id: str,
        work_item_id: str | None,
        conversation_id: str,
        disposition: str,
        reason: str,
        policy_version: str,
        body: str,
        idempotency_key: str,
        destination: dict[str, str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> PublicationDecision:
        if work_item_id is not None:
            self.get_work_item(work_item_id)
        self.get_conversation(conversation_id)
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if disposition not in {"suppress", "private_nudge", "group_update", "urgent_escalation"}:
            raise ValueError(f"unsupported publication disposition: {disposition}")
        if disposition != "suppress" and not body.strip():
            raise ValueError("published messages require a body")
        if destination is not None and any(not isinstance(key, str) or not key.strip()
                                           or not isinstance(value, str) or not value.strip()
                                           for key, value in destination.items()):
            raise ValueError("publication destination must contain non-empty string fields")
        clean_attachments: list[dict[str, Any]] = []
        if attachments is not None:
            if not isinstance(attachments, list) or len(attachments) > 10 or any(not isinstance(a, dict) for a in attachments):
                raise ValueError("publication attachments must be at most ten objects")
            clean_attachments = [dict(a) for a in attachments]
        now = self.now()
        outbox_id = None if disposition == "suppress" else self.new_id("outbox")
        decision = PublicationDecision(
            decision_id=decision_id,
            work_item_id=work_item_id,
            conversation_id=conversation_id,
            disposition=disposition,
            reason=reason,
            policy_version=policy_version,
            outbox_id=outbox_id,
            created_at=now,
        )
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT payload FROM publication_decisions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return PublicationDecision.from_dict(self.store.decode(existing[0]))
            try:
                connection.execute(
                    "INSERT INTO publication_decisions(decision_id, work_item_id, idempotency_key, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    (decision_id, work_item_id, idempotency_key, self.store.encode(decision.to_dict()), now),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    "SELECT payload FROM publication_decisions WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing:
                    return PublicationDecision.from_dict(self.store.decode(existing[0]))
                raise
            if outbox_id:
                outbox_destination = dict(destination or {})
                if (outbox_destination.get("channel") == "qq"
                        and outbox_destination.get("reply_to_message_id")):
                    self._assign_qq_reply_sequence(connection, conversation_id, outbox_destination)
                message = OutboxMessage(outbox_id, conversation_id, body, idempotency_key, "pending", now,
                                        outbox_destination, publication_disposition=disposition,
                                        attachments=clean_attachments)
                connection.execute(
                    "INSERT INTO outbox(outbox_id, idempotency_key, payload, status, created_at) VALUES (?, ?, ?, ?, ?)",
                    (outbox_id, idempotency_key, self.store.encode(message.to_dict()), message.status, now),
                )
            self._audit(connection, "publication_decision", decision_id, "decided", {
                "disposition": disposition,
                "reason": reason,
                "policy_version": policy_version,
                "outbox_id": outbox_id,
                **({"attachments": len(clean_attachments)} if clean_attachments else {}),
            }, now)
        return decision

    @staticmethod
    def _assign_qq_reply_sequence(connection: Any, conversation_id: str,
                                  destination: dict[str, str]) -> None:
        deadline = destination.get("reply_deadline_epoch")
        if deadline is not None:
            try:
                if time.time() >= float(deadline):
                    return
            except ValueError as error:
                raise ValueError("QQ reply deadline is invalid") from error
        row = connection.execute(
            "SELECT last_sequence FROM qq_reply_sequences WHERE conversation_id = ? AND reply_message_id = ?",
            (conversation_id, destination["reply_to_message_id"]),
        ).fetchone()
        sequence = int(row[0]) + 1 if row else 1
        if sequence > 5:
            # Exceeding QQ's 5-reply passive cap falls back cleanly to active delivery.
            destination.pop("reply_to_message_id", None)
            destination.pop("msg_seq", None)
            return
        if row:
            connection.execute(
                "UPDATE qq_reply_sequences SET last_sequence = ? WHERE conversation_id = ? AND reply_message_id = ?",
                (sequence, conversation_id, destination["reply_to_message_id"]),
            )
        else:
            connection.execute(
                "INSERT INTO qq_reply_sequences(conversation_id, reply_message_id, last_sequence) VALUES (?, ?, ?)",
                (conversation_id, destination["reply_to_message_id"], sequence),
            )
        destination["msg_seq"] = str(sequence)

    def list_outbox(self) -> list[OutboxMessage]:
        with self.store._transaction_lock:
            rows = self.store.connection.execute("SELECT payload FROM outbox ORDER BY rowid").fetchall()
        return [OutboxMessage.from_dict(self.store.decode(row[0])) for row in rows]

    def _transition_outbox(self, outbox_id: str, expected: set[str], status: str, action: str,
                           update: Any | None = None) -> OutboxMessage:
        with self.store.transaction() as connection:
            expected_states = tuple(sorted(expected))
            placeholders = ", ".join("?" for _ in expected_states)
            changed = connection.execute(
                f"UPDATE outbox SET status = ? WHERE outbox_id = ? AND status IN ({placeholders})",
                (status, outbox_id, *expected_states),
            ).rowcount
            if changed != 1:
                row = connection.execute(
                    "SELECT status FROM outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
                if row is None:
                    raise NotFound(f"outbox message not found: {outbox_id}")
                raise InvalidTransition(f"outbox is {row[0]}; expected {', '.join(expected_states)}")
            row = connection.execute(
                "SELECT payload FROM outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
            message = OutboxMessage.from_dict(self.store.decode(row[0]))
            message.status = status
            if update is not None:
                update(message)
            connection.execute(
                "UPDATE outbox SET payload = ? WHERE outbox_id = ? AND status = ?",
                (self.store.encode(message.to_dict()), outbox_id, status),
            )
            self._audit(connection, "outbox", outbox_id, action, {"attempts": message.attempts}, self.now())
            return message

    def mark_outbox_attempt(self, outbox_id: str) -> OutboxMessage:
        def begin(message: OutboxMessage) -> None:
            message.attempts += 1
            message.last_attempt_at = self.now()
            message.last_error = None

        return self._transition_outbox(outbox_id, {"pending", "failed"}, "sending", "sending", begin)

    def mark_outbox_failed(self, outbox_id: str, error: str) -> OutboxMessage:
        return self._transition_outbox(outbox_id, {"sending"}, "failed", "failed",
                                       lambda message: setattr(message, "last_error", error[:500]))

    def mark_outbox_sent(self, outbox_id: str) -> OutboxMessage:
        return self._transition_outbox(outbox_id, {"sending"}, "sent", "sent")

    def cancel_outbox(self, outbox_id: str, reason: str) -> OutboxMessage:
        return self._transition_outbox(outbox_id, {"pending", "failed"}, "cancelled", "cancelled",
                                       lambda message: setattr(message, "last_error", reason[:500]))

    def resolve_outbox_delivery(self, outbox_id: str, *, delivered: bool) -> OutboxMessage:
        status = "sent" if delivered else "pending"
        action = "manually_confirmed_sent" if delivered else "manually_requeued"
        return self._transition_outbox(outbox_id, {"sending"}, status, action,
                                       lambda message: setattr(message, "last_error", None))

    def recover_unfinished(self) -> None:
        rows = self.store.connection.execute(
            "SELECT payload FROM executions WHERE status = 'running'"
        ).fetchall()
        for row in rows:
            request = ExecutionRequest.from_dict(self.store.decode(row[0]))
            if request.capability == "dsh.task":
                # The dispatcher reconciles durable receipts under a live Workspace lease.
                continue
            now = self.now()
            request.status = "queued"
            request.updated_at = now
            plan = self._find_plan_for_work_item(request.work_item_id)
            step = next(step for step in plan.steps if step.step_id == request.plan_step_id)
            step.status = "ready"
            plan.updated_at = now
            item = self.get_work_item(request.work_item_id)
            if item.status == "running":
                item.status = "queued"
                item.updated_at = now
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE executions SET payload = ?, status = ?, updated_at = ? WHERE execution_id = ?",
                    (self.store.encode(request.to_dict()), request.status, now, request.execution_id),
                )
                self._save_plan(connection, plan)
                self._save_work_item(connection, item)
                self._audit(connection, "execution", request.execution_id, "recovered", {}, now)

        # Any queued execution whose owning assistant_run already terminated can never start.
        terminal_runs = {"failed", "completed", "observed", "filtered", "execution_failed"}
        queued_rows = self.store.connection.execute(
            "SELECT execution_id, work_item_id, payload FROM executions WHERE status = 'queued'"
        ).fetchall()
        for row in queued_rows:
            run_row = self.store.connection.execute(
                "SELECT event_id, status, payload FROM assistant_runs WHERE payload LIKE ?",
                (f'%"{row[1]}"%',),
            ).fetchone()
            if run_row is None:
                continue
            if str(run_row[1]) in terminal_runs:
                self.fail_queued_execution(
                    str(row[0]),
                    error="Execution remained queued after assistant run finished",
                    error_category="orphaned_queue",
                )

    def reconcile_abandoned_queued(self, manager: Any, *, min_age_seconds: float = 60.0) -> list[str]:
        """Mark stale queued executions as failed when no process holds their workspace lease."""
        from .execution_state import ExecutionBusy

        reconciled: list[str] = []
        now_dt = datetime.now(timezone.utc)
        rows = self.store.connection.execute(
            "SELECT execution_id, work_item_id, updated_at, payload FROM executions WHERE status = 'queued'"
        ).fetchall()
        for row in rows:
            execution_id = str(row[0])
            work_item_id = str(row[1])
            try:
                updated_dt = datetime.fromisoformat(str(row[2]))
            except ValueError:
                continue
            if (now_dt - updated_dt).total_seconds() < min_age_seconds:
                continue
            request = ExecutionRequest.from_dict(self.store.decode(row[3]))
            try:
                with manager.execution_lease(request.workspace_id, wait_seconds=0.0):
                    settled = self.fail_queued_execution(
                        execution_id,
                        error="Execution remained queued without an active workspace executor",
                        error_category="abandoned_queue",
                    )
            except (ExecutionBusy, OSError, ValueError):
                continue
            if settled is None:
                continue
            reconciled.append(execution_id)
            try:
                run = self.get_assistant_run_for_work_item(work_item_id)
            except NotFound:
                continue
            if run.status in {"executing", "authorizing"}:
                run.status = "failed"
                run.error_category = "abandoned_queue"
                run.error = "Execution remained queued without an active workspace executor"
                self.save_assistant_run(run)
        reconciled.extend(self.reconcile_interrupted_running(manager, min_age_seconds=min_age_seconds))
        return reconciled

    def reconcile_interrupted_running(self, manager: Any, *, min_age_seconds: float = 60.0) -> list[str]:
        """Settle stale running executions whose workspace lease is no longer held by any active process."""
        from .execution_state import ExecutionBusy
        from .runtime_config import POLICY_VERSION

        reconciled: list[str] = []
        now_dt = datetime.now(timezone.utc)
        rows = self.store.connection.execute(
            "SELECT execution_id, work_item_id, updated_at, payload FROM executions WHERE status = 'running'"
        ).fetchall()
        base_dir = getattr(manager, "base_directory", None)
        receipt_dir = (base_dir.parent / "runtime" / "receipts") if isinstance(base_dir, Path) else None
        for row in rows:
            execution_id = str(row[0])
            work_item_id = str(row[1])
            try:
                updated_dt = datetime.fromisoformat(str(row[2]))
            except ValueError:
                continue
            if (now_dt - updated_dt).total_seconds() < min_age_seconds:
                continue
            request = ExecutionRequest.from_dict(self.store.decode(row[3]))
            try:
                with manager.execution_lease(request.workspace_id, wait_seconds=0.0):
                    current = self.get_execution(execution_id)
                    if current.status != "running":
                        continue
                    result: ExecutionResult | None = None
                    if receipt_dir is not None:
                        receipt_path = receipt_dir / f"{execution_id}.json"
                        if receipt_path.is_file() and not receipt_path.is_symlink():
                            try:
                                envelope = json.loads(receipt_path.read_text(encoding="utf-8"))
                                candidate = ExecutionResult(**envelope["result"])
                                if candidate.execution_id == execution_id and candidate.status in {
                                    "succeeded", "failed", "timed_out", "cancelled",
                                }:
                                    result = candidate
                            except Exception:
                                result = None
                    if result is None:
                        result = ExecutionResult(
                            execution_id,
                            "failed",
                            error="Execution was interrupted without a result receipt; inspect its effects before creating a new request",
                            metadata={
                                "workspace_id": request.workspace_id,
                                "error_category": "interrupted",
                                "finished_at": self.now(),
                                "policy_version": POLICY_VERSION,
                            },
                        )
                    try:
                        ws = manager.get(request.workspace_id)
                        if ws.status in {"ready", "running", "recovering"}:
                            manager.save_execution_record(request.workspace_id, {
                                "execution_id": execution_id,
                                "runtime": "dsh",
                                "status": result.status,
                                "result": result.to_dict(),
                            })
                            if ws.status in {"running", "recovering"}:
                                manager.mark_ready(request.workspace_id)
                    except Exception:
                        pass
                    self.record_execution_result(result)
            except (ExecutionBusy, InvalidTransition, NotFound, OSError, ValueError):
                continue
            reconciled.append(execution_id)
            try:
                run = self.get_assistant_run_for_work_item(work_item_id)
            except NotFound:
                continue
            existing_ids = {
                r.get("execution_id") for r in run.execution_results if isinstance(r, dict)
            }
            if execution_id not in existing_ids:
                run.execution_results.append(result.to_dict())
            if run.status in {"executing", "authorizing"}:
                if result.status == "succeeded":
                    run.status = "completed"
                    run.error = None
                    run.error_category = None
                else:
                    run.status = "execution_failed"
                    run.error_category = str((result.metadata or {}).get("error_category") or "interrupted")
                    run.error = result.error or "Execution was interrupted"
                self.save_assistant_run(run)
        return reconciled

    def list_audit(self, *, entity_type: str, entity_id: str) -> list[AuditEntry]:
        rows = self.store.fetchall(
            "SELECT audit_id, entity_type, entity_id, action, payload, created_at FROM audit WHERE entity_type = ? AND entity_id = ? ORDER BY audit_id",
            (entity_type, entity_id),
        )
        return [AuditEntry.from_row((row[0], row[1], row[2], row[3], self.store.decode(row[4]), row[5])) for row in rows]

    def record_capability_gap(
        self,
        *,
        event_id: str,
        source_type: str,
        source_id: str,
        conversation_id: str,
        group_tier: str,
        topic: str,
        summary: str,
        missing_capability: str,
        suggested_solution: str,
        gap_kind: str = "other",
    ) -> dict[str, Any]:
        cleaned_topic = (topic or "").strip()[:200]
        cleaned_summary = (summary or "").strip()[:1000]
        cleaned_missing = (missing_capability or "").strip()[:300]
        cleaned_solution = (suggested_solution or "").strip()[:1000]
        if not event_id.strip() or not cleaned_topic or not cleaned_summary or not cleaned_missing or not cleaned_solution:
            raise ValueError("capability gap event_id, topic, summary, missing_capability, and suggested_solution are required")
        normalized_kind = gap_kind if gap_kind in {
            "tool_script", "python_package", "knowledge_access", "external_api", "other"
        } else "other"
        gap_id = f"gap-{hashlib.sha256(event_id.encode('utf-8')).hexdigest()[:24]}"
        now = self.now()
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT gap_id,event_id,source_type,source_id,conversation_id,group_tier,topic,summary,"
                "missing_capability,gap_kind,suggested_solution,status,resolution_note,created_at,updated_at "
                "FROM capability_gaps WHERE gap_id = ?",
                (gap_id,),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            connection.execute(
                "INSERT INTO capability_gaps(gap_id,event_id,source_type,source_id,conversation_id,"
                "group_tier,topic,summary,missing_capability,gap_kind,suggested_solution,status,"
                "resolution_note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    gap_id, event_id.strip(), source_type.strip(), source_id.strip(), conversation_id.strip(),
                    (group_tier or "client_group").strip()[:40], cleaned_topic, cleaned_summary,
                    cleaned_missing, normalized_kind, cleaned_solution, "open", "", now, now,
                ),
            )
            record = {
                "gap_id": gap_id,
                "event_id": event_id.strip(),
                "source_type": source_type.strip(),
                "source_id": source_id.strip(),
                "conversation_id": conversation_id.strip(),
                "group_tier": (group_tier or "client_group").strip()[:40],
                "topic": cleaned_topic,
                "summary": cleaned_summary,
                "missing_capability": cleaned_missing,
                "gap_kind": normalized_kind,
                "suggested_solution": cleaned_solution,
                "status": "open",
                "resolution_note": "",
                "created_at": now,
                "updated_at": now,
            }
            self._audit(connection, "capability_gap", gap_id, "recorded", {
                "topic": cleaned_topic,
                "missing_capability": cleaned_missing,
                "gap_kind": normalized_kind,
                "source_id": source_id.strip(),
            }, now)
            return record

    def list_capability_gaps(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(limit, 300))
        if status and status != "all":
            if status not in {"open", "resolved", "dismissed"}:
                raise ValueError("capability gap status must be open, resolved, or dismissed")
            rows = self.store.fetchall(
                "SELECT gap_id,event_id,source_type,source_id,conversation_id,group_tier,topic,summary,"
                "missing_capability,gap_kind,suggested_solution,status,resolution_note,created_at,updated_at "
                "FROM capability_gaps WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, bounded),
            )
        else:
            rows = self.store.fetchall(
                "SELECT gap_id,event_id,source_type,source_id,conversation_id,group_tier,topic,summary,"
                "missing_capability,gap_kind,suggested_solution,status,resolution_note,created_at,updated_at "
                "FROM capability_gaps ORDER BY created_at DESC LIMIT ?",
                (bounded,),
            )
        return [dict(row) for row in rows]

    def update_capability_gap_status(self, gap_id: str, *, status: str, resolution_note: str = "") -> dict[str, Any]:
        if status not in {"open", "resolved", "dismissed"}:
            raise ValueError("capability gap status must be open, resolved, or dismissed")
        now = self.now()
        cleaned_note = (resolution_note or "").strip()[:400]
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT gap_id FROM capability_gaps WHERE gap_id = ?", (gap_id,)
            ).fetchone()
            if existing is None:
                raise NotFound(f"capability gap not found: {gap_id}")
            connection.execute(
                "UPDATE capability_gaps SET status = ?, resolution_note = ?, updated_at = ? WHERE gap_id = ?",
                (status, cleaned_note, now, gap_id),
            )
            row = connection.execute(
                "SELECT gap_id,event_id,source_type,source_id,conversation_id,group_tier,topic,summary,"
                "missing_capability,gap_kind,suggested_solution,status,resolution_note,created_at,updated_at "
                "FROM capability_gaps WHERE gap_id = ?",
                (gap_id,),
            ).fetchone()
            self._audit(connection, "capability_gap", gap_id, status, {"resolution_note": cleaned_note}, now)
            return dict(row)

    def _find_plan_for_work_item(self, work_item_id: str) -> WorkPlan:
        row = self.store.connection.execute(
            "SELECT payload FROM plans WHERE work_item_id = ? ORDER BY rowid DESC LIMIT 1", (work_item_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"plan not found for work item: {work_item_id}")
        return WorkPlan.from_dict(self.store.decode(row[0]))

    @staticmethod
    def _save_work_item(connection: Any, item: WorkItem) -> None:
        payload = Store.encode(item.to_dict())
        connection.execute(
            "INSERT INTO work_items(work_item_id, payload, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(work_item_id) DO UPDATE SET payload = excluded.payload, status = excluded.status, updated_at = excluded.updated_at",
            (item.work_item_id, payload, item.status, item.created_at, item.updated_at),
        )

    @staticmethod
    def _save_plan(connection: Any, plan: WorkPlan) -> None:
        connection.execute(
            "UPDATE plans SET payload = ?, updated_at = ? WHERE plan_id = ?",
            (Store.encode(plan.to_dict()), plan.updated_at, plan.plan_id),
        )

    @staticmethod
    def _audit(connection: Any, entity_type: str, entity_id: str, action: str, payload: dict[str, Any], created_at: str) -> None:
        connection.execute(
            "INSERT INTO audit(entity_type, entity_id, action, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (entity_type, entity_id, action, Store.encode(payload), created_at),
        )
