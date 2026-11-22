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
    attachments: list[dict[str, Any]]
    capabilities: list[str]
    external_actor_id: str | None = None
    knowledge_grant: str | None = None

    def __post_init__(self) -> None:
        required = {
            "event_id": self.event_id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "conversation_id": self.conversation_id,
            "actor_id": self.actor_id,
            "occurred_at": self.occurred_at,
            "content": self.content,
        }
        if any(not isinstance(value, str) or not value.strip() for value in required.values()):
            raise ValueError("event required fields must be non-empty strings")
        if any(not isinstance(attachment, dict) for attachment in self.attachments):
            raise ValueError("event attachments must be objects")
        if any(not isinstance(capability, str) or not capability.strip() for capability in self.capabilities):
            raise ValueError("event capabilities must be non-empty strings")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("knowledge_grant", None)
        return value

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
class Conversation:
    conversation_id: str
    kind: str
    participant_refs: list[str]
    policy_version: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Conversation":
        return cls(**value)


@dataclass
class Thread:
    thread_id: str
    conversation_id: str
    title: str
    event_ids: list[str]
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Thread":
        return cls(**value)


@dataclass
class AssistantRun:
    event_id: str
    status: str
    policy_version: str = "unknown"
    conversation_id: str | None = None
    thread_id: str | None = None
    upstream_trigger_id: str | None = None
    upstream_thread_key: str | None = None
    decision_id: str | None = None
    decision: dict[str, Any] | None = None
    work_item_id: str | None = None
    plan_id: str | None = None
    execution_ids: list[str] = field(default_factory=list)
    execution_results: list[dict[str, Any]] = field(default_factory=list)
    publication: dict[str, Any] | None = None
    auto_authorized: bool = False
    workspace_id: str | None = None
    error_category: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AssistantRun":
        return cls(**value)


@dataclass
class Memory:
    memory_id: str
    key: str
    value: str
    source_event_ids: list[str]
    confidence: float
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Memory":
        return cls(**value)


@dataclass
class Approval:
    approval_id: str
    work_item_id: str
    scope: str
    policy_version: str
    status: str
    requested_at: str
    decided_at: str | None = None
    actor_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Approval":
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
    knowledge_document_ids: list[str] = field(default_factory=list)
    attachment_ids: list[str] = field(default_factory=list)

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
        data = dict(value)
        steps = [PlanStep.from_dict(step) for step in data.pop("steps")]
        return cls(steps=steps, **data)


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
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublicationDecision:
    decision_id: str
    work_item_id: str | None
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
    destination: dict[str, str] = field(default_factory=dict)
    attempts: int = 0
    last_attempt_at: str | None = None
    last_error: str | None = None
    publication_disposition: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OutboxMessage":
        compatible = {"destination": {}, "attempts": 0, "last_attempt_at": None, "last_error": None,
                      "publication_disposition": None, "attachments": []}
        compatible.update(value)
        return cls(**compatible)


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


GROUP_SOURCE_TYPES = frozenset({"qq.group", "qq.guild", "wecom.group"})
DIRECT_SOURCE_TYPES = frozenset({"qq.direct", "qq.private", "qq.c2c", "wecom.dm", "wecom.single"})
IM_SOURCE_TYPES = GROUP_SOURCE_TYPES | DIRECT_SOURCE_TYPES


def is_group_source(source_type: str) -> bool:
    return source_type in GROUP_SOURCE_TYPES


def is_direct_source(source_type: str) -> bool:
    return source_type in DIRECT_SOURCE_TYPES


def is_im_source(source_type: str) -> bool:
    return source_type in IM_SOURCE_TYPES


def source_provider(source_type: str) -> str:
    if source_type.startswith("qq."):
        return "qq"
    if source_type.startswith("wecom."):
        return "wecom"
    return ""


def source_identity_scope(source_type: str, source_id: str) -> str:
    return source_id if source_type in {"qq.group", "wecom.group"} else "*"

