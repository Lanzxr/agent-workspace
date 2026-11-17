from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any

from .control_plane import ControlPlane, InvalidTransition, NotFound
from .dssuperagent import (DEFAULT_DECISION_TIMEOUT_SECONDS, DssuperAgentAdapter, DssuperAgentError,
                           DssuperAgentProtocolError, DssuperAgentReceipt)
from .execution import ExecutionDispatcher
from .knowledge.service import KnowledgeService
from .models import AssistantRun, Event, ExecutionResult, PlanStep, WorkPlan
from .reply_contract import final_reply_markers
from .workspace import WorkspaceManager


@dataclass(frozen=True)
class AssistantPolicy:
    """Capabilities the backend may execute without a separate approval."""

    supported_capabilities: frozenset[str] = frozenset({"dsh.task"})
    auto_execute_capabilities: frozenset[str] = frozenset()
    version: str = ""

    def __post_init__(self) -> None:
        capabilities = self.supported_capabilities | self.auto_execute_capabilities
        invalid_version = bool(self.version) and not self.version.strip()
        if (invalid_version or any(not item.strip() for item in capabilities)
                or not self.auto_execute_capabilities.issubset(self.supported_capabilities)):
            raise ValueError("assistant policy requires a version and non-empty capability names")
        if not self.version:
            definition = {
                "supported_capabilities": sorted(self.supported_capabilities),
                "auto_execute_capabilities": sorted(self.auto_execute_capabilities),
            }
            digest = hashlib.sha256(json.dumps(definition, separators=(",", ":")).encode()).hexdigest()[:12]
            object.__setattr__(self, "version", f"assistant-policy-v1-{digest}")


class DssuperAgentWorkflow:
    """Turn upstream intent into durable control-plane work and authorized execution."""

    TERMINAL_RUN_STATUSES = {"observed", "proposed", "filtered", "completed", "execution_failed"}

    def __init__(
        self,
        plane: ControlPlane,
        upstream: DssuperAgentAdapter,
        *,
        manager: WorkspaceManager | None = None,
        dispatcher: ExecutionDispatcher | None = None,
        knowledge_service: KnowledgeService | None = None,
        policy: AssistantPolicy = AssistantPolicy(),
        decision_timeout: float = DEFAULT_DECISION_TIMEOUT_SECONDS,
        max_context_events: int = 10,
    ) -> None:
        if (manager is None) != (dispatcher is None):
            raise ValueError("WorkspaceManager and ExecutionDispatcher must be configured together")
        if decision_timeout <= 0 or not 0 <= max_context_events <= 10:
            raise ValueError("decision_timeout must be positive and context is limited to ten events")
        self.plane = plane
        self.upstream = upstream
        self.manager = manager
        self.dispatcher = dispatcher
        self.knowledge_service = knowledge_service or (dispatcher.knowledge_service if dispatcher else None)
        self.policy = policy
        self.decision_timeout = decision_timeout
        self.max_context_events = max_context_events

    def process_event(self, event: Event, *, workspace_id: str | None = None) -> AssistantRun:
        """Submit one event, persist its proposal, and auto-run only policy-allowed capabilities."""
        run = self._load_or_start(event)
        saved_event = self.plane.get_event(event.event_id)
        event = replace(saved_event, knowledge_grant=event.knowledge_grant)
        if run.status in self.TERMINAL_RUN_STATUSES:
            return run

        try:
            if run.policy_version != self.policy.version:
                raise InvalidTransition("assistant run was created under a different policy version")
            if run.decision is None:
                if run.upstream_trigger_id and run.upstream_thread_key:
                    receipt = DssuperAgentReceipt("duplicate", run.upstream_trigger_id, run.upstream_thread_key)
                else:
                    receipt = self.upstream.submit_event(event, context=self._context(event, run.thread_id))
                    run.upstream_trigger_id = receipt.trigger_id
                    run.upstream_thread_key = receipt.thread_key
                run.status = "awaiting_decision"
                self.plane.save_assistant_run(run)
                if receipt.status == "filtered":
                    run.status = "filtered"
                    return self.plane.save_assistant_run(run)
                decision = self.upstream.wait_for_decision(
                    receipt.thread_key, receipt.trigger_id, timeout=self.decision_timeout
                )
                if decision.kind != "ignore" or decision.outcome not in {"ignore", "wouldAccept"}:
                    raise DssuperAgentProtocolError("Company Agent events must remain proposals in DSSuperAgent")
                run.decision_id = decision.decision_id
                run.decision = decision.to_dict()
                run.publication = dict(decision.assistant_intent["publication"])
                run.status = "decided"
                self.plane.save_assistant_run(run)

            intent = run.decision["assistant_intent"]
            if intent["action"] == "observe":
                run.status = "observed"
                return self.plane.save_assistant_run(run)

            capabilities = self._validate_capabilities(event, intent)
            self._validate_knowledge_documents(event, intent)
            may_auto_execute = run.auto_authorized or (
                workspace_id is not None
                and self.manager is not None
                and self.dispatcher is not None
                and capabilities.issubset(self.policy.auto_execute_capabilities)
            )
            run = self._ensure_proposal(event, run, intent, risk_class="low" if may_auto_execute else "high")
            if not may_auto_execute:
                run.status = "proposed"
                return self.plane.save_assistant_run(run)

            if run.workspace_id is not None and workspace_id is not None and run.workspace_id != workspace_id:
                raise InvalidTransition("assistant run is already bound to a different workspace")
            workspace_id = run.workspace_id or workspace_id
            if not workspace_id or self.manager is None or self.dispatcher is None:
                raise ValueError("automatic execution requires a configured Workspace and DSH dispatcher")
            run.auto_authorized = True
            run.workspace_id = workspace_id
            run.status = "authorizing"
            self.plane.save_assistant_run(run)
            item = self.plane.get_work_item(run.work_item_id or "")
            if item.status == "proposed":
                self.plane.accept_work_item(item.work_item_id)
                self.plane.queue_work_item(item.work_item_id)
            elif item.status == "candidate":
                self.plane.propose_work_item(item.work_item_id)
                self.plane.accept_work_item(item.work_item_id)
                self.plane.queue_work_item(item.work_item_id)
            run.status = "executing"
            self.plane.save_assistant_run(run)
            return self._execute(run, event, workspace_id, {})
        except (DssuperAgentError, InvalidTransition, NotFound, ValueError, OSError) as error:
            run.status = "failed"
            run.error_category = getattr(error, "category", "workflow_error")
            run.error = str(error)
            self.plane.save_assistant_run(run)
            raise

    def execute_accepted_work(
        self,
        work_item_id: str,
        workspace_id: str,
        *,
        approval_refs: dict[str, str] | None = None,
    ) -> AssistantRun:
        """Resume an operator-authorized proposal; approvals are still checked by the control plane."""
        if not workspace_id.strip():
            raise ValueError("workspace_id is required")
        if self.manager is None or self.dispatcher is None:
            raise ValueError("DSH execution is not configured")
        run = self.plane.get_assistant_run_for_work_item(work_item_id)
        if run.policy_version != self.policy.version:
            raise InvalidTransition("assistant run was created under a different policy version")
        event = self.plane.get_event(run.event_id)
        item = self.plane.get_work_item(work_item_id)
        if item.status == "accepted":
            self.plane.queue_work_item(work_item_id)
        elif item.status not in {"queued", "running", "completed"}:
            raise InvalidTransition(f"work item is {item.status}; only accepted work can be executed")
        if run.workspace_id is not None and run.workspace_id != workspace_id:
            raise InvalidTransition("assistant run is already bound to a different workspace")
        run.workspace_id = workspace_id
        run.status = "executing"
        run.error = None
        run.error_category = None
        self.plane.save_assistant_run(run)
        return self._execute(run, event, workspace_id, approval_refs or {})

    def record_event_context(self, event: Event) -> Event:
        """Persist an event in its conversation before attachment or model work starts."""
        try:
            saved_event = self.plane.get_event(event.event_id)
        except NotFound:
            self.plane.ingest_event(event)
            saved_event = event
        else:
            immutable = ("source_type", "source_id", "conversation_id", "actor_id", "occurred_at", "content",
                         "capabilities", "external_actor_id")
            if any(getattr(saved_event, name) != getattr(event, name) for name in immutable):
                raise ValueError("event_id already exists with conflicting event content")

        conversation_id, thread_id = self._local_thread_ids(event)
        try:
            conversation = self.plane.get_conversation(conversation_id)
        except NotFound:
            self.plane.create_conversation(
                conversation_id=conversation_id,
                kind=event.source_type,
                participant_refs=[event.actor_id],
                policy_version=self.policy.version,
            )
        try:
            thread = self.plane.get_thread(thread_id)
        except NotFound:
            self.plane.create_thread(thread_id=thread_id, conversation_id=conversation_id,
                                     title=event.conversation_id[:160])
        else:
            if thread.conversation_id != conversation_id:
                raise ValueError("local thread is associated with a different conversation")
        self.plane.attach_event_to_thread(thread_id, event.event_id)
        return replace(saved_event, knowledge_grant=event.knowledge_grant)

    def _load_or_start(self, event: Event) -> AssistantRun:
        self.record_event_context(event)
        conversation_id, thread_id = self._local_thread_ids(event)
        try:
            return self.plane.get_assistant_run(event.event_id)
        except NotFound:
            return self.plane.save_assistant_run(AssistantRun(
                event_id=event.event_id,
                status="pending",
                policy_version=self.policy.version,
                conversation_id=conversation_id,
                thread_id=thread_id,
            ))

    def _context(self, event: Event, thread_id: str | None) -> list[dict[str, Any]]:
        if not thread_id or self.max_context_events == 0:
            return []
        thread = self.plane.get_thread(thread_id)
        prior = [event_id for event_id in thread.event_ids if event_id != event.event_id]
        return [
            {"event_id": item.event_id, "actor_id": item.actor_id,
             "occurred_at": item.occurred_at, "content": self._context_content(item)}
            for item in (self.plane.get_event(event_id) for event_id in prior[-self.max_context_events:])
        ]

    @staticmethod
    def _context_content(event: Event) -> str:
        """Keep prior attachments visible to DSSuperAgent without widening its schema.

        Historical context is intentionally sent through the existing text-only
        context contract. Only non-secret metadata is included; attachment URLs
        and any provider-specific fields never cross this boundary.
        """
        content = event.content[:12000]
        if not event.attachments:
            return content
        details: list[str] = []
        for index, attachment in enumerate(event.attachments[:10], start=1):
            if not isinstance(attachment, dict):
                continue
            fields: list[str] = []
            for key in ("attachment_id", "filename", "content_type", "occurred_at", "received_at", "status",
                        "document_id", "content_hash", "size_bytes", "sensitive"):
                value = attachment.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    fields.append(f"{key}={str(value)[:300]}")
            if fields:
                details.append(f"附件 {index}: " + "; ".join(fields))
        if not details:
            return content
        suffix = "\n[历史消息附件信息]\n" + "\n".join(details)
        return (content + suffix)[:12000]

    @staticmethod
    def _local_thread_ids(event: Event) -> tuple[str, str]:
        identity = "\0".join((event.source_type, event.source_id, event.conversation_id)).encode("utf-8")
        suffix = hashlib.sha256(identity).hexdigest()[:28]
        return f"company-conversation-{suffix}", f"company-thread-{suffix}"

    def _validate_capabilities(self, event: Event, intent: dict[str, Any]) -> set[str]:
        capabilities = {step["capability"] for step in intent["plan_steps"]}
        if not capabilities.issubset(set(event.capabilities)):
            raise DssuperAgentProtocolError("proposed plan uses a capability not present in the event envelope")
        if not capabilities.issubset(self.policy.supported_capabilities):
            raise DssuperAgentProtocolError("proposed plan uses a capability with no configured execution adapter")
        if not capabilities:
            raise DssuperAgentProtocolError("proposed work has no executable capability")
        return capabilities

    def _validate_knowledge_documents(self, event: Event, intent: dict[str, Any]) -> None:
        steps = intent.get("plan_steps", [])
        selected = [document_id for step in steps for document_id in step.get("knowledge_document_ids", [])]
        selected_attachments = [attachment_id for step in steps for attachment_id in step.get("attachment_ids", [])]
        if not selected and not selected_attachments:
            return
        service = self.knowledge_service
        if service is None:
            raise DssuperAgentProtocolError("DSSuperAgent selected files but the knowledge service is not configured")
        if selected:
            service.authorize_documents(event, list(dict.fromkeys(selected)))
        if selected_attachments:
            service.authorize_attachments(event, list(dict.fromkeys(selected_attachments)), require_original=False)

    def _ensure_proposal(self, event: Event, run: AssistantRun, intent: dict[str, Any], *, risk_class: str) -> AssistantRun:
        suffix = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:24]
        work_item_id = f"assistant-work-{suffix}"
        plan_id = f"assistant-plan-{suffix}"
        try:
            item = self.plane.get_work_item(work_item_id)
        except NotFound:
            self.plane.create_work_item(
                work_item_id=work_item_id,
                title=intent["title"],
                objective=intent["objective"],
                source_event_ids=[event.event_id],
                completion_conditions=intent["completion_conditions"],
                risk_class=risk_class,
                owner_ref=event.actor_id,
            )
            item = self.plane.propose_work_item(work_item_id)
        else:
            if item.source_event_ids != [event.event_id] or item.title != intent["title"] or item.objective != intent["objective"]:
                raise ValueError("saved work item does not match the DSSuperAgent proposal")
            if item.status == "candidate":
                item = self.plane.propose_work_item(work_item_id)
        run.work_item_id = work_item_id
        run.plan_id = plan_id
        run.publication = dict(intent["publication"])
        run.status = "mapping_proposal"
        return self.plane.save_assistant_run(run)

    def _execute(
        self,
        run: AssistantRun,
        event: Event,
        workspace_id: str,
        approval_refs: dict[str, str],
    ) -> AssistantRun:
        if self.manager is None or self.dispatcher is None or not run.work_item_id or not run.plan_id:
            raise ValueError("assistant run is missing its DSH execution configuration")
        intent = (run.decision or {}).get("assistant_intent")
        if not isinstance(intent, dict):
            raise ValueError("assistant run has no persisted intent")
        self._validate_capabilities(event, intent)
        self._validate_knowledge_documents(event, intent)
        self.manager.provision(workspace_id)
        qq_reply = event.source_type.startswith("qq.")
        item = self.plane.get_work_item(run.work_item_id)
        if item.status == "accepted":
            self.plane.queue_work_item(item.work_item_id)
        elif item.status not in {"queued", "running", "completed"}:
            raise InvalidTransition(f"work item is {item.status}, not authorized for execution")
        plan = self._get_or_create_plan(run)
        final_ordinal = max((step.ordinal for step in plan.steps), default=0)
        for step in plan.steps:
            execution_id = self._execution_id(event.event_id, step.ordinal)
            artifact_name = f"executions/{execution_id}/step-{step.ordinal:02d}-result.md"
            reply_token = execution_id if qq_reply and step.ordinal == final_ordinal else None
            try:
                request = self.plane.get_execution(execution_id)
            except NotFound:
                knowledge_document_ids = list(dict.fromkeys(step.knowledge_document_ids))
                attachment_ids = list(dict.fromkeys(step.attachment_ids))
                request = self.plane.create_execution_request(
                    execution_id=execution_id,
                    work_item_id=run.work_item_id,
                    plan_step_id=step.step_id,
                    workspace_id=workspace_id,
                    capability=step.capability or "",
                    parameters={
                        "prompt": self._step_prompt(step, artifact_name, qq_reply=qq_reply,
                                                    reply_token=reply_token),
                        "expected_artifacts": [artifact_name],
                        "timeout_seconds": 300 if qq_reply else 240,
                        "knowledge_document_ids": knowledge_document_ids,
                        "attachment_ids": attachment_ids,
                        "require_user_reply": reply_token is not None,
                    },
                    approval_ref=approval_refs.get(step.capability or ""),
                )
            if request.workspace_id != workspace_id:
                raise InvalidTransition("saved execution is bound to a different workspace")
            if execution_id not in run.execution_ids:
                run.execution_ids.append(execution_id)
                run.status = "executing"
                self.plane.save_assistant_run(run)
            result = self.dispatcher.dispatch(execution_id)
            record = self._execution_record(run, execution_id)
            record.update({
                "status": result.status,
                "output": result.output[:4000],
                "artifact_refs": result.artifact_refs,
                "error": result.error,
            })
            run.status = "feedback_pending"
            self.plane.save_assistant_run(run)
            if "feedback_trigger_id" not in record:
                feedback = self.upstream.submit_execution_result(self._result_event(event, run, result))
                if feedback.status == "filtered":
                    raise DssuperAgentProtocolError("DSSuperAgent filtered an execution result event")
                record["feedback_trigger_id"] = feedback.trigger_id
                record["feedback_thread_key"] = feedback.thread_key
                self.plane.save_assistant_run(run)
            if result.status != "succeeded":
                run.status = "execution_failed"
                run.error_category = str(result.metadata.get("error_category") or "execution_failed")
                run.error = result.error or "DSH execution did not succeed"
                return self.plane.save_assistant_run(run)
        run.status = "completed"
        run.error = None
        run.error_category = None
        return self.plane.save_assistant_run(run)

    def _get_or_create_plan(self, run: AssistantRun) -> WorkPlan:
        try:
            plan = self.plane.get_plan(run.plan_id or "")
        except NotFound:
            intent = (run.decision or {}).get("assistant_intent")
            if not isinstance(intent, dict):
                raise ValueError("assistant run has no persisted plan intent")
            plan_steps = []
            suffix = hashlib.sha256(run.event_id.encode("utf-8")).hexdigest()[:20]
            for ordinal, value in enumerate(intent["plan_steps"], start=1):
                plan_steps.append(PlanStep(
                    step_id=f"assistant-{suffix}-{ordinal}",
                    ordinal=ordinal,
                    title=value["title"],
                    objective=value["objective"],
                    capability=value["capability"],
                    completion_conditions=value.get("completion_conditions", []),
                    knowledge_document_ids=value.get("knowledge_document_ids", []),
                    attachment_ids=value.get("attachment_ids", []),
                ))
            plan = self.plane.create_plan(plan_id=run.plan_id or "", work_item_id=run.work_item_id or "",
                                          steps=plan_steps)
        return plan

    @staticmethod
    def _execution_id(event_id: str, ordinal: int) -> str:
        suffix = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
        return f"assistant_{suffix}_{ordinal}"

    @staticmethod
    def _step_prompt(step: PlanStep, artifact_name: str, *, qq_reply: bool = False,
                     reply_token: str | None = None) -> str:
        conditions = step.completion_conditions or [step.objective or step.title]
        lines = [step.objective or step.title, "", "Completion conditions:", *(f"- {item}" for item in conditions),
                 "", f"Write a concise evidence report to artifacts/{artifact_name}."]
        if qq_reply:
            lines.extend([
                "",
                "执行边界：只在当前 Agent Workspace 内处理文件并写入指定报告，不得向 QQ 或其他外部渠道发送消息。",
                "Company Agent 负责向 QQ 用户发送结果。报告应使用中文，不要把发送状态、执行过程、工具记录或交给 Company Agent 的指示当成答复内容。",
            ])
        if reply_token is not None:
            begin, end = final_reply_markers(reply_token)
            lines.extend([
                "",
                "最终答复格式：在报告中恰好写入以下一对标记，且只把可直接回复用户的完整结论放在标记之间。",
                begin,
                "（在此写给用户的最终结论，最多 1500 个汉字和标点；不要写执行说明、发送状态或内部日志。）",
                end,
                "标记外的文字不会发送；缺少标记或内容超长会使本次任务被判定为未完成。",
            ])
        return "\n".join(lines)

    @staticmethod
    def _execution_record(run: AssistantRun, execution_id: str) -> dict[str, Any]:
        record = next((item for item in run.execution_results if item.get("execution_id") == execution_id), None)
        if record is None:
            record = {"execution_id": execution_id}
            run.execution_results.append(record)
        return record

    @staticmethod
    def _result_event(event: Event, run: AssistantRun, result: ExecutionResult) -> Event:
        payload = {
            "event_type": "execution_result",
            "source_event_id": event.event_id,
            "work_item_id": run.work_item_id,
            "execution_id": result.execution_id,
            "status": result.status,
            "output": result.output[:4000],
            "error": result.error[:1000] if result.error else None,
            "evidence_refs": [value[:200] for value in result.evidence_refs[:20]],
            "artifact_refs": [value[:200] for value in result.artifact_refs[:20]],
        }
        if len(result.output) > 4000 or len(result.evidence_refs) > 20 or len(result.artifact_refs) > 20:
            payload["output_truncated"] = True
        return Event(
            event_id=f"company-agent-result-{result.execution_id}",
            source_type="company-agent.execution-result",
            source_id=result.execution_id,
            conversation_id=event.conversation_id,
            actor_id="company-agent",
            occurred_at=ControlPlane.now(),
            content=json.dumps(payload, ensure_ascii=False),
            attachments=[],
            capabilities=[],
        )
