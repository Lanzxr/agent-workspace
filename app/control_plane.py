from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import (
    AuditEntry,
    Event,
    ExecutionRequest,
    ExecutionResult,
    OutboxMessage,
    PlanStep,
    PublicationDecision,
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
    "candidate": {"proposed", "accepted", "cancelled"},
    "proposed": {"accepted", "candidate", "cancelled"},
    "accepted": {"queued", "blocked", "cancelled"},
    "queued": {"running", "blocked", "cancelled"},
    "running": {"completed", "failed", "blocked", "cancelled"},
    "blocked": {"accepted", "queued", "cancelled"},
    "failed": {"accepted", "queued", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


class ControlPlane:
    def __init__(self, store: Store) -> None:
        self.store = store

    @classmethod
    def open(cls, path: Path) -> "ControlPlane":
        return cls(Store(path))

    def close(self) -> None:
        self.store.close()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}-{uuid4()}"

    def ingest_event(self, event: Event) -> bool:
        created_at = self.now()
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if existing:
                return False
            connection.execute(
                "INSERT INTO events(event_id, payload, created_at) VALUES (?, ?, ?)",
                (event.event_id, self.store.encode(event.to_dict()), created_at),
            )
            self._audit(connection, "event", event.event_id, "ingested", {}, created_at)
        return True

    def get_event(self, event_id: str) -> Event:
        row = self.store.connection.execute(
            "SELECT payload FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"event not found: {event_id}")
        return Event.from_dict(self.store.decode(row[0]))

    def create_work_item(
        self,
        *,
        work_item_id: str,
        title: str,
        objective: str,
        source_event_ids: list[str],
        completion_conditions: list[str],
        risk_class: str,
        owner_ref: str | None = None,
    ) -> WorkItem:
        if not source_event_ids:
            raise ValueError("source_event_ids cannot be empty")
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

    def queue_work_item(self, work_item_id: str) -> WorkItem:
        return self._transition_work_item(work_item_id, "queued")

    def start_work_item(self, work_item_id: str) -> WorkItem:
        return self._transition_work_item(work_item_id, "running")

    def complete_work_item(self, work_item_id: str) -> WorkItem:
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
        if item.status not in {"accepted", "queued", "running"}:
            raise InvalidTransition(f"cannot execute work item in {item.status}")
        plan = self._find_plan_for_work_item(work_item_id)
        step = next((candidate for candidate in plan.steps if candidate.step_id == plan_step_id), None)
        if step is None:
            raise NotFound(f"plan step not found: {plan_step_id}")
        if step.status != "pending":
            raise InvalidTransition(f"plan step is already {step.status}")
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

    def start_execution(self, execution_id: str) -> ExecutionRequest:
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
        if item.status == "queued":
            item.status = "running"
            item.updated_at = request.updated_at
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE executions SET payload = ?, status = ?, updated_at = ? WHERE execution_id = ?",
                (self.store.encode(request.to_dict()), request.status, request.updated_at, execution_id),
            )
            self._save_plan(connection, plan)
            self._save_work_item(connection, item)
            self._audit(connection, "execution", execution_id, "started", {}, request.updated_at)
        return request

    def record_execution_result(self, result: ExecutionResult) -> ExecutionRequest:
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
        if result.status == "succeeded" and all(candidate.status == "completed" for candidate in plan.steps):
            item.status = "completed"
            item.updated_at = now
        elif result.status != "succeeded":
            item.status = "failed"
            item.updated_at = now
        with self.store.transaction() as connection:
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
    ) -> PublicationDecision:
        self.get_work_item(work_item_id)
        if disposition not in {"suppress", "private_nudge", "group_update", "urgent_escalation"}:
            raise ValueError(f"unsupported publication disposition: {disposition}")
        if disposition != "suppress" and not body.strip():
            raise ValueError("published messages require a body")
        existing = self.store.connection.execute(
            "SELECT payload FROM publication_decisions WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing:
            return PublicationDecision.from_dict(self.store.decode(existing[0]))
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
            connection.execute(
                "INSERT INTO publication_decisions(decision_id, work_item_id, idempotency_key, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (decision_id, work_item_id, idempotency_key, self.store.encode(decision.to_dict()), now),
            )
            if outbox_id:
                message = OutboxMessage(outbox_id, conversation_id, body, idempotency_key, "pending", now)
                connection.execute(
                    "INSERT INTO outbox(outbox_id, idempotency_key, payload, status, created_at) VALUES (?, ?, ?, ?, ?)",
                    (outbox_id, idempotency_key, self.store.encode(message.to_dict()), message.status, now),
                )
            self._audit(connection, "publication_decision", decision_id, "decided", {"disposition": disposition}, now)
        return decision

    def list_outbox(self) -> list[OutboxMessage]:
        rows = self.store.connection.execute("SELECT payload FROM outbox ORDER BY rowid").fetchall()
        return [OutboxMessage.from_dict(self.store.decode(row[0])) for row in rows]

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
