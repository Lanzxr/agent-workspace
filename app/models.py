from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    event_id: str
    source_type: str
    source_id: str
    conversation_id: str
    actor_id: str
    occurred_at: str
    content: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Event":
        return cls(**value)


@dataclass
class WorkItem:
    work_item_id: str
    title: str
    objective: str
    status: str
    source_event_ids: list[str]
    completion_conditions: list[str]
    risk_class: str
    owner_ref: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkItem":
        return cls(**value)


@dataclass
class PlanStep:
    step_id: str
    ordinal: int
    title: str
    objective: str = ""
    status: str = "pending"
    capability: str | None = None
    completion_conditions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlanStep":
        return cls(**value)


@dataclass
class WorkPlan:
    plan_id: str
    work_item_id: str
    steps: list[PlanStep]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["steps"] = [step.to_dict() for step in self.steps]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkPlan":
        steps = [PlanStep.from_dict(step) for step in value.pop("steps")]
        return cls(steps=steps, **value)


@dataclass
class ExecutionRequest:
    execution_id: str
    work_item_id: str
    plan_step_id: str
    workspace_id: str
    capability: str
    parameters: dict[str, Any]
    status: str
    created_at: str
    updated_at: str
    approval_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExecutionRequest":
        return cls(**value)


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    status: str
    output: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublicationDecision:
    decision_id: str
    work_item_id: str
    conversation_id: str
    disposition: str
    reason: str
    policy_version: str
    outbox_id: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PublicationDecision":
        return cls(**value)


@dataclass
class OutboxMessage:
    outbox_id: str
    conversation_id: str
    body: str
    idempotency_key: str
    status: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OutboxMessage":
        return cls(**value)


@dataclass(frozen=True)
class AuditEntry:
    audit_id: int
    entity_type: str
    entity_id: str
    action: str
    payload: dict[str, Any]
    created_at: str

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> "AuditEntry":
        audit_id, entity_type, entity_id, action, payload, created_at = row
        return cls(audit_id, entity_type, entity_id, action, payload, created_at)

