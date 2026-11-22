from __future__ import annotations

import hashlib
import inspect
import json
import sys
from dataclasses import dataclass, replace
from typing import Any, Callable, Sequence

from .control_plane import ControlPlane, ControlPlaneError, InvalidTransition, NotFound
from .dssuperagent import (DEFAULT_DECISION_TIMEOUT_SECONDS, DssuperAgentAdapter, DssuperAgentError,
                           DssuperAgentProtocolError, DssuperAgentReceipt)
from .execution import ExecutionDispatcher
from .execution_state import ExecutionBusy
from .identity import IdentityRegistry
from .knowledge.service import KnowledgeService
from .mcp_registry import McpServerSpec
from .models import AssistantRun, Event, ExecutionResult, PlanStep, WorkItem, WorkPlan, is_group_source, is_im_source
from .reply_contract import deliverable_files_markers, final_reply_markers
from .shared_tools import SharedToolsManager
from .tool_forge import ToolForgeService
from .workspace import WorkspaceError, WorkspaceManager
from .workspace_retention import get_retention_days, summarize_recent_workspace_files


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
        shared_tools: SharedToolsManager | None = None,
        tool_forge: ToolForgeService | None = None,
        mcp_servers_provider: Callable[[], Sequence[McpServerSpec]] | None = None,
        policy: AssistantPolicy = AssistantPolicy(),
        decision_timeout: float = DEFAULT_DECISION_TIMEOUT_SECONDS,
        max_context_events: int = 10,
        workspace_wait_seconds: float = 1800.0,
        on_execution_started: Callable[[Event, AssistantRun, WorkItem], None] | None = None,
    ) -> None:
        if (manager is None) != (dispatcher is None):
            raise ValueError("WorkspaceManager and ExecutionDispatcher must be configured together")
        if decision_timeout <= 0 or not 0 <= max_context_events <= 10 or workspace_wait_seconds < 0:
            raise ValueError("decision_timeout must be positive, context <= 10, and workspace_wait_seconds >= 0")
        self.plane = plane
        self.upstream = upstream
        self.manager = manager
        self.dispatcher = dispatcher
        self.knowledge_service = knowledge_service or (dispatcher.knowledge_service if dispatcher else None)
        if shared_tools is not None:
            self.shared_tools = shared_tools
        elif manager is not None and (manager.base_directory.parent / "shared-tools").is_dir():
            self.shared_tools = SharedToolsManager(manager.base_directory.parent / "shared-tools")
        else:
            self.shared_tools = None
        self.tool_forge = tool_forge
        adapter_mcp = getattr(getattr(dispatcher, "adapter", None), "all_mcp_servers", None)
        self.mcp_servers_provider = mcp_servers_provider or (adapter_mcp if callable(adapter_mcp) else None)
        self.policy = policy
        self.decision_timeout = decision_timeout
        self.max_context_events = max_context_events
        self.workspace_wait_seconds = float(workspace_wait_seconds)
        self.on_execution_started = on_execution_started

    def process_event(
        self,
        event: Event,
        *,
        workspace_id: str | None = None,
        interaction_mode: str = "direct",
    ) -> AssistantRun:
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
                    receipt = self._submit_to_upstream(
                        event,
                        context=self._context(event, run.thread_id),
                        interaction_mode=interaction_mode,
                        available_capabilities=self._capability_summary(
                            event, workspace_id=run.workspace_id or workspace_id,
                        ),
                    )
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
                if (decision.assistant_intent.get("silent_ack") is True
                        or (interaction_mode == "ambient_cooldown"
                            and decision.assistant_intent.get("action") == "propose_work"
                            and run.publication.get("recommendation") != "suppress")):
                    run.publication["silent_ack"] = True
                run.status = "decided"
                self.plane.save_assistant_run(run)

            intent = run.decision["assistant_intent"]
            if intent["action"] == "observe":
                gap = intent.get("capability_gap")
                if isinstance(gap, dict):
                    self.plane.record_capability_gap(
                        event_id=event.event_id,
                        source_type=event.source_type,
                        source_id=event.source_id,
                        conversation_id=event.conversation_id,
                        group_tier=self._scope_tier_for_event(event),
                        topic=str(gap.get("topic") or ""),
                        summary=str(gap.get("summary") or ""),
                        missing_capability=str(gap.get("missing_capability") or ""),
                        gap_kind=str(gap.get("gap_kind") or "other"),
                        suggested_solution=str(gap.get("suggested_solution") or ""),
                    )
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
        except (DssuperAgentError, ExecutionBusy, WorkspaceError, InvalidTransition, NotFound, ValueError, OSError) as error:
            run.status = "failed"
            run.error_category = (
                "workspace_busy"
                if isinstance(error, ExecutionBusy)
                else getattr(error, "category", "workflow_error")
            )
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
        try:
            return self._execute(run, event, workspace_id, approval_refs or {})
        except (DssuperAgentError, ExecutionBusy, WorkspaceError, InvalidTransition, NotFound, ValueError, OSError) as error:
            run.status = "failed"
            run.error_category = (
                "workspace_busy"
                if isinstance(error, ExecutionBusy)
                else getattr(error, "category", "workflow_error")
            )
            run.error = str(error)
            self.plane.save_assistant_run(run)
            raise

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

    def _submit_to_upstream(
        self,
        event: Event,
        *,
        context: list[dict[str, Any]],
        interaction_mode: str,
        available_capabilities: dict[str, Any],
    ) -> DssuperAgentReceipt:
        fn = self.upstream.submit_event
        params = inspect.signature(fn).parameters
        kwargs: dict[str, Any] = {"context": context}
        accepts_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if accepts_varkw or "interaction_mode" in params:
            kwargs["interaction_mode"] = interaction_mode
        if accepts_varkw or "available_capabilities" in params:
            kwargs["available_capabilities"] = available_capabilities
        return fn(event, **kwargs)

    def _scope_tier_for_event(self, event: Event) -> str:
        identities = IdentityRegistry(self.plane.store)
        if is_group_source(event.source_type):
            group = identities.get_group(event.source_type, event.source_id)
            return "internal" if (group is not None and group.tier == "internal") else "client_group"
        ks_settings = getattr(self.knowledge_service, "settings", None)
        admin_ids = getattr(ks_settings, "admin_user_ids", frozenset())
        role = identities.user_role(event.actor_id, admin_ids)
        if role == "admin":
            return "admin"
        if role == "employee":
            return "internal"
        return "direct"

    def _capability_summary(self, event: Event, *, workspace_id: str | None = None) -> dict[str, Any]:
        tier = self._scope_tier_for_event(event)
        can_read_internal = tier in {"internal", "admin"}
        summary: dict[str, Any] = {
            "tier": tier,
            "can_read_internal_knowledge": can_read_internal,
            "downward_compatibility": bool(self.plane.get_setting("downward_compatibility_enabled", True)),
            "web_search_enabled": bool(self.plane.get_setting("web_search_enabled", False)),
            "sandbox_network": "disabled (--unshare-net; use shared-tools scripts or web_search/web_fetch)",
            "bot_aliases": list(self.plane.get_setting("bot_aliases", ["憨憨", "机器人", "小助手", "AI助手", "助手"]) or []),
            "workspace_file_retention_days": get_retention_days(self.plane),
        }
        if self.knowledge_service is not None:
            summary_fn = getattr(self.knowledge_service, "summary", None)
            docs = summary_fn().get("documents", {}) if callable(summary_fn) else {}
            summary["knowledge_configured"] = True
            summary["knowledge_ready_documents"] = int(docs.get("ready", 0))
        else:
            summary["knowledge_configured"] = False
        if self.shared_tools is not None:
            tools_meta = self.shared_tools.summary().get("layers", {})
            visible_layers = ("common", "internal") if can_read_internal else ("common",)
            tools_out: dict[str, Any] = {}
            for layer in visible_layers:
                layer_data = tools_meta.get(layer, {})
                tools_out[layer] = {
                    "scripts": [
                        f"{s['name']} ({s['description']})" if s.get("description") else s["name"]
                        for s in (layer_data.get("scripts") or [])[:25]
                    ],
                    "packages": [p.get("spec") for p in (layer_data.get("packages") or [])[:25] if p.get("spec")],
                }
            summary["shared_tools"] = tools_out
        if self.mcp_servers_provider is not None:
            authorized_mcp = [spec for spec in self.mcp_servers_provider() if spec.allows_tier(tier)]
            summary["mcp_servers"] = [spec.to_summary_dict() for spec in authorized_mcp]
            if authorized_mcp:
                summary["sandbox_network"] = (
                    "disabled (--unshare-net; use authorized MCP tools (mcp__*), shared-tools scripts, or web_search/web_fetch)"
                )
        if self.tool_forge is not None:
            if tier == "admin":
                summary["tool_forge_enabled"] = True
                summary["tool_forge_pending_proposals"] = len(self.tool_forge.list_proposals(status="pending", limit=20))
            mcp_server_id = (
                "forge_admin" if tier == "admin" else ("forge_internal" if tier == "internal" else "forge_common")
            )
            active_custom = self.tool_forge.list_active_tools_for_tier(tier)
            if active_custom:
                summary["custom_tools"] = [
                    {
                        "tool_id": item["tool_id"],
                        "kind": item["kind"],
                        "operation_type": item["operation_type"],
                        "visibility": item["visibility"],
                        "mcp_server": mcp_server_id,
                        "mcp_tool": f"mcp__{mcp_server_id}__{item['tool_id']}",
                        "description": item["description"],
                        "when_to_use": item["when_to_use"],
                        "when_not_to_use": item["when_not_to_use"],
                    }
                    for item in active_custom[:40]
                ]
        if self.manager is not None and workspace_id:
            try:
                retained = summarize_recent_workspace_files(
                    self.manager, workspace_id, retention_days=get_retention_days(self.plane),
                )
            except Exception:
                retained = []
            if retained:
                summary["recent_workspace_files"] = retained
        return summary

    def _context(self, event: Event, thread_id: str | None) -> list[dict[str, Any]]:
        if not thread_id or self.max_context_events == 0:
            return []
        thread = self.plane.get_thread(thread_id)
        prior_ids = [event_id for event_id in thread.event_ids if event_id != event.event_id][-self.max_context_events:]
        entries: list[dict[str, Any]] = []
        reply_keys = [f"qq-publication:{eid}" for eid in prior_ids]
        replies_by_key: dict[str, tuple[str, str]] = {}
        if reply_keys:
            placeholders = ",".join("?" for _ in reply_keys)
            rows = self.plane.store.fetchall(
                f"SELECT idempotency_key,payload,created_at FROM outbox WHERE idempotency_key IN ({placeholders})",
                reply_keys,
            )
            for row in rows:
                try:
                    op = self.plane.store.decode(row["payload"])
                    body = str(op.get("body") or "").strip()
                    if body:
                        replies_by_key[str(row["idempotency_key"])] = (body, str(row["created_at"]))
                except (TypeError, ValueError):
                    continue
        for eid in prior_ids:
            item = self.plane.get_event(eid)
            entries.append({
                "event_id": item.event_id,
                "role": "user",
                "actor_id": item.actor_id,
                "occurred_at": item.occurred_at,
                "content": self._context_content(item),
            })
            reply = replies_by_key.get(f"qq-publication:{eid}")
            if reply is not None:
                body, created_at = reply
                entries.append({
                    "role": "assistant",
                    "actor_id": "company-agent",
                    "occurred_at": created_at,
                    "content": body[:4000],
                })
        return entries[-self.max_context_events:]

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
        silent = bool((run.publication or {}).get("silent_ack")) or (intent.get("silent_ack") is True)
        run.publication = dict(intent["publication"])
        if silent:
            run.publication["silent_ack"] = True
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
        qq_reply = is_im_source(event.source_type)
        item = self.plane.get_work_item(run.work_item_id)
        if item.status == "accepted":
            self.plane.queue_work_item(item.work_item_id)
        elif item.status not in {"queued", "running", "completed"}:
            raise InvalidTransition(f"work item is {item.status}, not authorized for execution")
        plan = self._get_or_create_plan(run)
        if self.on_execution_started is not None:
            try:
                self.on_execution_started(event, run, item)
            except Exception as error:
                print(f"company-agent: execution start callback failed ({type(error).__name__})", file=sys.stderr)
        adapter_config = getattr(getattr(self.dispatcher, "adapter", None), "config", None)
        default_timeout = float(getattr(adapter_config, "dsh_default_timeout_seconds", 3600))
        default_idle_timeout = float(getattr(adapter_config, "dsh_idle_timeout_seconds", 600))
        default_compaction_timeout = float(getattr(adapter_config, "dsh_compaction_timeout_seconds", 600))
        final_ordinal = max((step.ordinal for step in plan.steps), default=0)
        output_dir = f"work/outputs/{run.work_item_id}"
        try:
            retained_files = summarize_recent_workspace_files(
                self.manager, workspace_id, retention_days=get_retention_days(self.plane), limit=8,
            )
        except Exception:
            retained_files = []
        prior_artifacts: list[str] = []
        inherited_document_ids: list[str] = []
        inherited_attachment_ids: list[str] = []
        active_session_id: str | None = None
        for step in plan.steps:
            execution_id = self._execution_id(event.event_id, step.ordinal)
            artifact_name = f"executions/{execution_id}/step-{step.ordinal:02d}-result.md"
            reply_token = execution_id if qq_reply and step.ordinal == final_ordinal else None
            resume_sid = active_session_id if (step.capability == "dsh.task" and active_session_id) else None
            for doc_id in step.knowledge_document_ids:
                if doc_id and doc_id not in inherited_document_ids:
                    inherited_document_ids.append(doc_id)
            for att_id in step.attachment_ids:
                if att_id and att_id not in inherited_attachment_ids:
                    inherited_attachment_ids.append(att_id)
            try:
                request = self.plane.get_execution(execution_id)
            except NotFound:
                knowledge_document_ids = list(dict.fromkeys([*step.knowledge_document_ids, *inherited_document_ids]))
                attachment_ids = list(dict.fromkeys([*step.attachment_ids, *inherited_attachment_ids]))
                step_params: dict[str, Any] = {
                    "prompt": self._step_prompt(step, artifact_name, qq_reply=qq_reply,
                                                reply_token=reply_token,
                                                prior_artifacts=prior_artifacts,
                                                resumed_session=resume_sid is not None,
                                                output_dir=output_dir,
                                                retained_files=retained_files),
                    "expected_artifacts": [artifact_name],
                    "timeout_seconds": default_timeout,
                    "idle_timeout_seconds": default_idle_timeout,
                    "compaction_timeout_seconds": default_compaction_timeout,
                    "knowledge_document_ids": knowledge_document_ids,
                    "attachment_ids": attachment_ids,
                    "require_user_reply": reply_token is not None,
                }
                if resume_sid is not None:
                    step_params["resume_session_id"] = resume_sid
                request = self.plane.create_execution_request(
                    execution_id=execution_id,
                    work_item_id=run.work_item_id,
                    plan_step_id=step.step_id,
                    workspace_id=workspace_id,
                    capability=step.capability or "",
                    parameters=step_params,
                    approval_ref=approval_refs.get(step.capability or ""),
                )
            if request.workspace_id != workspace_id:
                raise InvalidTransition("saved execution is bound to a different workspace")
            if execution_id not in run.execution_ids:
                run.execution_ids.append(execution_id)
                run.status = "executing"
                self.plane.save_assistant_run(run)
            dispatch_fn = self.dispatcher.dispatch
            dispatch_params = inspect.signature(dispatch_fn).parameters
            try:
                if "wait_seconds" in dispatch_params:
                    result = dispatch_fn(execution_id, wait_seconds=self.workspace_wait_seconds)
                else:
                    result = dispatch_fn(execution_id)
            except Exception as error:
                error_category = (
                    "workspace_busy"
                    if isinstance(error, ExecutionBusy)
                    else getattr(error, "category", type(error).__name__)
                )
                try:
                    self.plane.fail_queued_execution(
                        execution_id,
                        error=str(error),
                        error_category=str(error_category),
                    )
                except (ControlPlaneError, NotFound, ValueError):
                    pass
                raise
            meta_dict = result.metadata if isinstance(result.metadata, dict) else {}
            meta_sid = meta_dict.get("session_id")
            if isinstance(meta_sid, str) and meta_sid.strip():
                active_session_id = meta_sid.strip()
            for ref in result.artifact_refs:
                if isinstance(ref, str) and ref and ref not in prior_artifacts:
                    prior_artifacts.append(ref)
            if f"artifacts/{artifact_name}" not in prior_artifacts:
                prior_artifacts.append(f"artifacts/{artifact_name}")
            deliverables = meta_dict.get("deliverable_files")
            record = self._execution_record(run, execution_id)
            record.update({
                "status": result.status,
                "output": result.output[:4000],
                "artifact_refs": result.artifact_refs,
                "error": result.error,
                **({"session_id": active_session_id} if active_session_id else {}),
                **({"deliverable_files": deliverables} if isinstance(deliverables, list) and deliverables else {}),
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
                     reply_token: str | None = None,
                     prior_artifacts: list[str] | tuple[str, ...] = (),
                     resumed_session: bool = False,
                     output_dir: str = "work/outputs",
                     retained_files: list[dict[str, Any]] | tuple[dict[str, Any], ...] = ()) -> str:
        conditions = step.completion_conditions or [step.objective or step.title]
        lines = [
            step.objective or step.title,
            "",
            "Completion conditions:",
            *(f"- {item}" for item in conditions),
        ]
        if resumed_session:
            lines.extend([
                "",
                "Session context: You are resuming the prior step's DSH Worker session; build directly on its findings instead of re-reading unchanged files.",
            ])
        if prior_artifacts:
            lines.extend([
                "",
                "Prior-step artifact path(s) (read ONLY these paths if needed; do not scan directories):",
                *(f"- {path}" for path in prior_artifacts),
            ])
        if retained_files:
            lines.extend([
                "",
                "Retained workspace files from recent turns in this conversation (read only if referenced by the task):",
                *(f"- `{item['path']}` ({item['filename']}, {item['category']})" for item in retained_files),
            ])
        lines.extend([
            "",
            "工具边界：当前 bash 沙箱断网隔离；如需访问公开网页或 API，请直接使用内置 web_fetch / web_search 工具，不要在 bash 中用 curl 或 wget 测试外网。不要运行 ls/find 扫描 work/、tools/ 或无关历史目录。",
            "执行节奏：同类结构勘查或数据检查请尽量合并在单次脚本中完成，避免拆成多次零碎调用；核心产出校验通过后立即写入指定报告并结束，避免多轮反复微调次要排版。",
            f"交付文件目录：若任务需要生成交付给用户的新文件（如 .xlsx、.pdf、.docx、.csv 等），请保存到 `{output_dir}/` 目录内（勿写入旧版 work/out/）。",
            f"Write a concise evidence report to artifacts/{artifact_name} (create parent dirs, write content, and verify length in one bash command). 即使遇到缺文件或工具阻塞无法继续，也必须通过 bash 将明确的中文阻塞原因写入上述报告文件后再结束，禁止只输出思考或纯文本而不写报告。",
        ])
        if qq_reply:
            lines.extend([
                "",
                "执行边界：只在当前 Agent Workspace 内处理文件并写入指定报告，不得向 QQ 或其他外部渠道发送消息。",
                "Company Agent 负责向 QQ 用户发送结果与上传文件。报告应使用中文，不要把发送状态、执行过程、工具记录或交给 Company Agent 的指示当成答复内容。",
            ])
        if reply_token is not None:
            begin, end = final_reply_markers(reply_token)
            fbegin, fend = deliverable_files_markers(reply_token)
            lines.extend([
                "",
                "最终答复格式：在报告中恰好写入以下一对标记，且只把可直接回复用户的完整结论放在标记之间。",
                begin,
                "（在此写给用户的最终结论，最多 1500 个汉字和标点；不要写执行说明、发送状态或内部日志。）",
                end,
                "标记外的文字不会发送；缺少标记或内容超长会使本次任务被判定为未完成。",
                "",
                f"交付文件声明：仅当本次任务实际生成了需要上传发给 QQ 群的真实文件时（纯文字答复请勿写入此标记），才在报告中写入 {fbegin} 和 {fend}，中间放相对路径 JSON 数组（如对应 `{output_dir}/...` 下真实存在的文件，最多 5 个）。",
            ])
        else:
            lines.extend([
                "",
                "当前为多步流程的中间阶段：只需将本阶段关键结果、数据或结论精炼写入目标产物，无需编写面向 QQ 用户的最终长篇答复或起止标记。",
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
