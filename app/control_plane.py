from __future__ import annotations

import sqlite3
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
    "queued": {"running", "blocked", "cancelled"},
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
        row = self.store.connection.execute("SELECT payload FROM settings WHERE setting_key=?", (key,)).fetchone()
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
        work_item_id: str,
        conversation_id: str,
        disposition: str,
        reason: str,
        policy_version: str,
        body: str,
        idempotency_key: str,
        destination: dict[str, str] | None = None,
    ) -> PublicationDecision:
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
                message = OutboxMessage(outbox_id, conversation_id, body, idempotency_key, "pending", now,
                                        destination or {}, publication_disposition=disposition)
                connection.execute(
                    "INSERT INTO outbox(outbox_id, idempotency_key, payload, status, created_at) VALUES (?, ?, ?, ?, ?)",
                    (outbox_id, idempotency_key, self.store.encode(message.to_dict()), message.status, now),
                )
            self._audit(connection, "publication_decision", decision_id, "decided", {"disposition": disposition}, now)
        return decision

    def list_outbox(self) -> list[OutboxMessage]:
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

    def list_audit(self, *, entity_type: str, entity_id: str) -> list[AuditEntry]:
        rows = self.store.connection.execute(
            "SELECT audit_id, entity_type, entity_id, action, payload, created_at FROM audit WHERE entity_type = ? AND entity_id = ? ORDER BY audit_id",
            (entity_type, entity_id),
        ).fetchall()
        return [AuditEntry.from_row((row[0], row[1], row[2], row[3], self.store.decode(row[4]), row[5])) for row in rows]

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
