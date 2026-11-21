from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.assistant_workflow import AssistantPolicy, DssuperAgentWorkflow
from app.control_plane import ControlPlane, ControlPlaneError
from app.dsh import DshAdapter
from app.dssuperagent import (DssuperAgentDecision, DssuperAgentProtocolError, DssuperAgentReceipt,
                              DssuperAgentTimeout)
from app.execution import ExecutionDispatcher
from app.models import Event, ExecutionResult
from app.runtime_config import RuntimeConfig
from app.workspace import WorkspaceManager


def propose_decision() -> DssuperAgentDecision:
    return DssuperAgentDecision(
        decision_id="decision-1",
        trigger_ids=["company-agent:event-1"],
        thread_key="company-agent:conversation-1",
        kind="ignore",
        outcome="ignore",
        reason="A useful next action is clear",
        occurred_at="2026-11-15T00:00:00Z",
        strength="inferred",
        assistant_intent={
            "action": "propose_work",
            "rationale": "The discussion needs a verifiable answer",
            "title": "Analyze the discussion",
            "objective": "Produce a concise evidence-based analysis",
            "completion_conditions": ["A report is saved"],
            "plan_steps": [{
                "title": "Prepare the analysis",
                "objective": "Summarize the evidence and conclusion",
                "capability": "dsh.task",
                "completion_conditions": ["The report identifies evidence and uncertainty"],
            }],
            "publication": {
                "recommendation": "group_update",
                "reason": "A concise conclusion would help the group",
                "body": "Analysis is complete.",
            },
        },
    )


class FakeDssuperAgent:
    def __init__(self, decision: DssuperAgentDecision | None = None) -> None:
        self.decision = decision or propose_decision()
        self.submitted: list[tuple[Event, list[dict[str, object]] | None]] = []
        self.results: list[Event] = []
        self.wait_calls = 0
        self.wait_timeouts: list[float] = []
        self.fail_first_wait = False

    def submit_event(self, event: Event, *, context: list[dict[str, object]] | None = None) -> DssuperAgentReceipt:
        self.submitted.append((event, context))
        return DssuperAgentReceipt("stored", f"company-agent:{event.event_id}",
                                   f"company-agent:{event.conversation_id}")

    def wait_for_decision(self, thread_key: str, trigger_id: str, *, timeout: float = 120) -> DssuperAgentDecision:
        self.wait_calls += 1
        self.wait_timeouts.append(timeout)
        if self.fail_first_wait and self.wait_calls == 1:
            raise DssuperAgentTimeout("decision arrived after the first wait expired")
        return self.decision

    def submit_execution_result(self, event: Event, *, context: list[dict[str, object]] | None = None) -> DssuperAgentReceipt:
        self.results.append(event)
        return DssuperAgentReceipt("stored", f"company-agent:{event.event_id}",
                                   f"company-agent:{event.conversation_id}")


class AssistantWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.manager = WorkspaceManager(self.root / "workspaces")
        self.plane = ControlPlane.open(self.root / "control.sqlite")
        self.config_file = self.root / "runtime.env"
        self.config_file.write_text("private test configuration", encoding="utf-8")
        self.config = RuntimeConfig(
            "https://models.example/v1", "fixture-model", "test-secret-key",
            (sys.executable, str(Path(__file__).parent / "fixtures" / "dsh_stub.py")), self.config_file,
        )
        self.bubblewrap = self.root / "bwrap"
        self.bubblewrap.write_text("test fixture", encoding="utf-8")
        self.bubblewrap.chmod(0o700)
        self.adapter = DshAdapter(self.config, self.manager, self.root / "runtime",
                                  bubblewrap_executable=self.bubblewrap)
        self.dispatcher = ExecutionDispatcher(self.plane, self.manager, self.adapter, self.root / "runtime")
        self.upstream = FakeDssuperAgent()

    def tearDown(self) -> None:
        self.plane.close()
        self.temporary_directory.cleanup()

    @staticmethod
    def event(*, event_id: str = "event-1", content: str = "A group discussion", capabilities: list[str] | None = None) -> Event:
        return Event(event_id, "chat", "channel-1", "conversation-1", "user-1",
                     "2026-11-15T00:00:00Z", content,
                     [], ["dsh.task"] if capabilities is None else capabilities)

    def workflow(self, *, auto_execute: bool = False) -> DssuperAgentWorkflow:
        policy = AssistantPolicy(auto_execute_capabilities=frozenset({"dsh.task"}) if auto_execute else frozenset())
        return DssuperAgentWorkflow(self.plane, self.upstream, manager=self.manager,
                                    dispatcher=self.dispatcher, policy=policy)

    def test_observation_is_persisted_without_creating_work_or_publication(self) -> None:
        decision = replace(propose_decision(), assistant_intent={
            "action": "observe", "rationale": "There is no useful action yet", "completion_conditions": [],
            "plan_steps": [], "publication": {"recommendation": "suppress", "reason": "No update is needed"},
        })
        self.upstream.decision = decision

        run = self.workflow().process_event(self.event())

        self.assertEqual(run.status, "observed")
        self.assertIsNone(run.work_item_id)
        self.assertEqual(self.plane.store.connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 0)
        self.assertEqual(self.plane.list_outbox(), [])
        self.assertEqual(self.plane.get_assistant_run("event-1").status, "observed")

    def test_follow_up_context_keeps_prior_attachment_metadata(self) -> None:
        observed = replace(propose_decision(), assistant_intent={
            "action": "observe", "rationale": "Record the prior upload", "completion_conditions": [],
            "plan_steps": [], "publication": {"recommendation": "suppress", "reason": "No update"},
        })
        self.upstream.decision = observed
        prior = self.event(event_id="event-upload", content="[附件]")
        prior = replace(prior, attachments=[{
            "filename": "test_us_news.docx", "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "status": "ready", "document_id": "doc-123", "size_bytes": 16451,
        }])
        self.workflow().process_event(prior)

        self.upstream.decision = propose_decision()
        follow_up = self.event(event_id="event-follow-up", content="这个文件里面是什么")
        self.workflow().process_event(follow_up)

        context = self.upstream.submitted[-1][1]
        self.assertIsNotNone(context)
        self.assertIn("历史消息附件信息", context[0]["content"])
        self.assertIn("filename=test_us_news.docx", context[0]["content"])
        self.assertIn("document_id=doc-123", context[0]["content"])

    def test_late_decision_reuses_the_persisted_dssuperagent_trigger(self) -> None:
        self.upstream.fail_first_wait = True
        workflow = self.workflow()

        with self.assertRaises(DssuperAgentTimeout):
            workflow.process_event(self.event())
        failed = self.plane.get_assistant_run("event-1")

        resumed = workflow.process_event(self.event())

        self.assertEqual(failed.status, "failed")
        self.assertEqual(resumed.status, "proposed")
        self.assertEqual(self.upstream.wait_calls, 2)
        self.assertEqual(len(self.upstream.submitted), 1)
        self.assertEqual(self.upstream.wait_timeouts, [600, 600])

    def test_proposal_is_idempotent_and_does_not_grant_execution_or_publication(self) -> None:
        workflow = self.workflow()

        first = workflow.process_event(self.event())
        second = workflow.process_event(self.event())

        self.assertEqual(first.status, "proposed")
        self.assertEqual(first.work_item_id, second.work_item_id)
        self.assertEqual(self.plane.get_work_item(first.work_item_id or "").status, "proposed")
        self.assertEqual(self.plane.get_work_item(first.work_item_id or "").risk_class, "high")
        self.assertEqual(len(self.upstream.submitted), 1)
        self.assertEqual(self.plane.list_outbox(), [])
        self.assertEqual(first.publication["recommendation"], "group_update")
        self.assertEqual(first.policy_version, workflow.policy.version)
        audit = self.plane.list_audit(entity_type="assistant_run", entity_id="event-1")[-1]
        self.assertEqual(audit.payload["policy_version"], workflow.policy.version)
        self.assertEqual(audit.payload["decision_id"], first.decision_id)
        self.assertEqual(audit.payload["upstream_trigger_id"], first.upstream_trigger_id)
        self.assertEqual(audit.payload["work_item_id"], first.work_item_id)
        with self.assertRaises(ControlPlaneError):
            self.plane.save_assistant_run(replace(first, policy_version="different-policy"))

    def test_proposal_document_ids_are_authorized_without_a_dsh_dispatcher(self) -> None:
        decision = propose_decision()
        decision.assistant_intent["plan_steps"][0]["knowledge_document_ids"] = ["document-1"]
        self.upstream.decision = decision

        class KnowledgeServiceStub:
            def __init__(self) -> None:
                self.authorized: list[tuple[str, list[str]]] = []

            def authorize_documents(self, event: Event, document_ids: list[str]) -> None:
                self.authorized.append((event.event_id, document_ids))

        knowledge_service = KnowledgeServiceStub()
        workflow = DssuperAgentWorkflow(self.plane, self.upstream, knowledge_service=knowledge_service)

        run = workflow.process_event(self.event())

        self.assertEqual(run.status, "proposed")
        self.assertEqual(knowledge_service.authorized, [("event-1", ["document-1"])])

    def test_ready_current_attachments_are_authorized_and_staged_for_dsh(self) -> None:
        class KnowledgeServiceStub:
            def __init__(self) -> None:
                self.authorized: list[list[str]] = []

            def authorize_documents(self, event: Event, document_ids: list[str]) -> None:
                self.authorized.append(document_ids)

        class CapturingDispatcher:
            knowledge_service = None

            def __init__(self, plane: ControlPlane) -> None:
                self.plane = plane
                self.document_ids: list[str] = []

            def dispatch(self, execution_id: str):
                request = self.plane.get_execution(execution_id)
                self.document_ids = request.parameters["knowledge_document_ids"]
                return ExecutionResult(execution_id, "succeeded", artifact_refs=["artifacts/result.md"])

        knowledge_service = KnowledgeServiceStub()
        dispatcher = CapturingDispatcher(self.plane)
        workflow = DssuperAgentWorkflow(
            self.plane,
            self.upstream,
            manager=self.manager,
            dispatcher=dispatcher,
            knowledge_service=knowledge_service,
            policy=AssistantPolicy(auto_execute_capabilities=frozenset({"dsh.task"})),
        )
        event = replace(self.event(), attachments=[
            {"filename": "minutes.md", "status": "ready", "document_id": "document-current"},
            {"filename": "broken.md", "status": "failed", "document_id": "document-broken"},
        ])

        selected = propose_decision()
        selected_intent = dict(selected.assistant_intent)
        selected_steps = [dict(step) for step in selected_intent["plan_steps"]]
        selected_steps[0]["knowledge_document_ids"] = ["document-current"]
        selected_intent["plan_steps"] = selected_steps
        self.upstream.decision = replace(selected, assistant_intent=selected_intent)

        run = workflow.process_event(event, workspace_id="assistant-workspace")

        self.assertEqual(run.status, "completed")
        self.assertEqual(dispatcher.document_ids, ["document-current"])
        self.assertEqual(knowledge_service.authorized, [["document-current"], ["document-current"]])

    def test_unlisted_capability_is_rejected_before_a_work_item_is_created(self) -> None:
        with self.assertRaises(DssuperAgentProtocolError):
            self.workflow().process_event(self.event(capabilities=[]))

        self.assertEqual(self.plane.store.connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 0)
        self.assertEqual(self.plane.get_assistant_run("event-1").status, "failed")

    def test_auto_authorized_proposal_runs_in_workspace_and_returns_result_upstream_once(self) -> None:
        workflow = self.workflow(auto_execute=True)
        self.assertNotEqual(workflow.policy.version, AssistantPolicy().version)

        qq_event = replace(self.event(), source_type="qq.group")
        run = workflow.process_event(qq_event, workspace_id="assistant-workspace")
        repeated = workflow.process_event(qq_event, workspace_id="assistant-workspace")

        self.assertEqual(run.status, "completed")
        self.assertEqual(repeated.status, "completed")
        self.assertEqual(self.plane.get_work_item(run.work_item_id or "").status, "completed")
        self.assertEqual(self.plane.get_plan(run.plan_id or "").steps[0].status, "completed")
        self.assertEqual(len(run.execution_ids), 1)
        audit = self.plane.list_audit(entity_type="assistant_run", entity_id="event-1")[-1]
        self.assertEqual(audit.payload["policy_version"], workflow.policy.version)
        self.assertTrue(audit.payload["auto_authorized"])
        self.assertEqual(audit.payload["execution_ids"], run.execution_ids)
        result = self.plane.get_execution_result(run.execution_ids[0])
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.artifact_refs, [f"artifacts/executions/{run.execution_ids[0]}/step-01-result.md"])
        request = self.plane.get_execution(run.execution_ids[0])
        self.assertEqual(request.parameters["timeout_seconds"], 3600.0)
        self.assertEqual(request.parameters["idle_timeout_seconds"], 600.0)
        self.assertEqual(request.parameters["compaction_timeout_seconds"], 600.0)
        self.assertTrue(request.parameters["require_user_reply"])
        self.assertIn(f"COMPANY_AGENT_FINAL_REPLY_BEGIN:{run.execution_ids[0]}", request.parameters["prompt"])
        self.assertIn("bash 沙箱断网隔离", request.parameters["prompt"])
        self.assertIn("web_fetch / web_search", request.parameters["prompt"])
        self.assertIn("Company Agent 负责向 QQ 用户发送结果", request.parameters["prompt"])
        self.assertEqual(run.execution_results[0]["output"], result.output[:4000])
        self.assertEqual((self.manager.get("assistant-workspace").root / "work/launch-count.txt").read_text(), "run\n")
        self.assertEqual(len(self.upstream.results), 1)
        feedback = self.upstream.results[0]
        self.assertEqual(feedback.source_type, "company-agent.execution-result")
        self.assertEqual(json.loads(feedback.content)["status"], "succeeded")

    def test_distinct_events_keep_execution_artifacts_separate_in_persistent_workspace(self) -> None:
        workflow = self.workflow(auto_execute=True)

        first = workflow.process_event(self.event(event_id="event-first"), workspace_id="assistant-workspace")
        self.upstream.decision = replace(
            propose_decision(),
            decision_id="decision-second",
            trigger_ids=["company-agent:event-second"],
        )
        second = workflow.process_event(self.event(event_id="event-second"), workspace_id="assistant-workspace")

        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        first_result = self.plane.get_execution_result(first.execution_ids[0])
        second_result = self.plane.get_execution_result(second.execution_ids[0])
        self.assertEqual(first_result.status, "succeeded")
        self.assertEqual(second_result.status, "succeeded")
        self.assertNotEqual(first_result.artifact_refs, second_result.artifact_refs)
        for result in (first_result, second_result):
            artifact = self.manager.get("assistant-workspace").root / result.artifact_refs[0]
            self.assertTrue(artifact.is_file())

    def test_high_risk_proposal_executes_only_after_control_plane_approval(self) -> None:
        workflow = self.workflow()
        run = workflow.process_event(self.event())
        work_item_id = run.work_item_id or ""
        self.plane.request_work_item_approval(work_item_id, "Operator authorized DSH execution")
        self.plane.request_approval(approval_id="approval-1", work_item_id=work_item_id,
                                    scope="dsh.task", policy_version=workflow.policy.version)
        self.plane.approve("approval-1", "operator-1")
        self.plane.accept_work_item(work_item_id)

        completed = workflow.execute_accepted_work(work_item_id, "assistant-workspace",
                                                   approval_refs={"dsh.task": "approval-1"})

        self.assertEqual(completed.status, "completed")
        self.assertEqual(self.plane.get_work_item(work_item_id).status, "completed")
        self.assertEqual(self.plane.get_approval("approval-1").status, "approved")

    def test_on_execution_started_callback_runs_before_dispatcher(self) -> None:
        order: list[str] = []

        class RecordingDispatcher:
            knowledge_service = None

            def dispatch(self, execution_id: str):
                order.append("dispatch")
                return ExecutionResult(execution_id, "succeeded", artifact_refs=[])

        workflow = DssuperAgentWorkflow(
            self.plane,
            self.upstream,
            manager=self.manager,
            dispatcher=RecordingDispatcher(),
            policy=AssistantPolicy(auto_execute_capabilities=frozenset({"dsh.task"})),
            on_execution_started=lambda event, run, item: order.append(f"ack:{event.event_id}:{item.title}"),
        )
        run = workflow.process_event(self.event(), workspace_id="assistant-workspace")
        self.assertEqual(run.status, "completed")
        self.assertEqual(order, ["ack:event-1:Analyze the discussion", "dispatch"])

    def test_context_includes_bot_replies_capability_summary_respects_tier_and_records_gap(self) -> None:
        from app.identity import IdentityRegistry
        from app.shared_tools import SharedToolsManager

        tools = SharedToolsManager(self.root / "shared-tools")
        tools.save_script("common", "pub.sh", "#!/bin/sh\necho ok\n", description="public helper")
        tools.save_script("internal", "calc_margin.py", "print(1)\n", description="internal margin calc")

        identities = IdentityRegistry(self.plane.store)
        identities.update_group(source_type="qq.group", source_id="room-1", tier="client_group")

        # Capturing upstream that records kwargs
        calls: list[dict] = []

        class InspectUpstream:
            def __init__(self, decision):
                self.decision = decision

            def submit_event(self, event, *, context=None, interaction_mode=None, available_capabilities=None):
                calls.append({
                    "event": event,
                    "context": context,
                    "interaction_mode": interaction_mode,
                    "available_capabilities": available_capabilities,
                })
                return DssuperAgentReceipt("stored", f"company-agent:{event.event_id}",
                                           f"company-agent:{event.conversation_id}")

            def wait_for_decision(self, _tk, tid, *, timeout=120):
                return self.decision

        gap_decision = DssuperAgentDecision(
            decision_id="dec-gap",
            trigger_ids=["company-agent:event-gap-1"],
            thread_key="company-agent:conversation-1",
            kind="ignore",
            outcome="ignore",
            reason="Company Agent retains control",
            occurred_at="2026-11-19T00:00:00Z",
            strength="inferred",
            assistant_intent={
                "action": "observe",
                "rationale": "Missing script in client tier",
                "completion_conditions": [],
                "plan_steps": [],
                "capability_gap": {
                    "topic": "套餐利润率核算",
                    "summary": "群内询问利润率",
                    "missing_capability": "calc_margin.py 未对当前区开放",
                    "gap_kind": "tool_script",
                    "suggested_solution": "授予或开放对应脚本后可自动计算",
                },
                "publication": {"recommendation": "suppress", "reason": "Stay silent"},
            },
        )
        ev1 = Event(
            "event-gap-1", "qq.group", "room-1", "conversation-1", "member-1",
            "2026-11-19T00:00:00Z", "这个月利润率怎么算？", [], ["dsh.task"],
        )
        wf = DssuperAgentWorkflow(
            self.plane, InspectUpstream(gap_decision), manager=self.manager,
            dispatcher=RecordingDispatcherForTest(self.plane, self.manager),
            shared_tools=tools,
        )
        run1 = wf.process_event(ev1, workspace_id="ws-c1", interaction_mode="ambient_cooldown")
        self.assertEqual(run1.status, "observed")
        # Client group must NEVER see internal layer scripts in available_capabilities
        caps = calls[-1]["available_capabilities"]
        self.assertEqual(caps["tier"], "client_group")
        self.assertIn("common", caps["shared_tools"])
        self.assertNotIn("internal", caps["shared_tools"])
        self.assertEqual(calls[-1]["interaction_mode"], "ambient_cooldown")
        gaps = self.plane.list_capability_gaps(status="open")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["topic"], "套餐利润率核算")

        # Promote group -> internal and simulate bot reply saved in outbox for ev1, then verify follow-up context sees bot reply & internal tools!
        identities.update_group(source_type="qq.group", source_id="room-1", tier="internal")
        self.plane.decide_publication(
            decision_id="pub-ev1",
            work_item_id=None,
            conversation_id=run1.conversation_id or "conversation-1",
            disposition="group_update",
            reason="bot replied",
            policy_version=wf.policy.version,
            body="机器人上一条回复：请提供月份。",
            idempotency_key=f"qq-publication:{ev1.event_id}",
            destination={"channel": "qq", "group_openid": "room-1"},
        )
        ev2 = Event(
            "event-followup-2", "qq.group", "room-1", "conversation-1", "member-1",
            "2026-11-19T00:01:00Z", "11月份的", [], ["dsh.task"],
        )
        wf.upstream = InspectUpstream(DssuperAgentDecision(
            decision_id="dec-ok",
            trigger_ids=["company-agent:event-followup-2"],
            thread_key="company-agent:conversation-1",
            kind="ignore",
            outcome="ignore",
            reason="Company Agent retains control",
            occurred_at="2026-11-19T00:01:00Z",
            strength="explicit",
            assistant_intent={
                "action": "observe",
                "rationale": "Direct reply",
                "completion_conditions": [],
                "plan_steps": [],
                "publication": {"recommendation": "group_update", "reason": "Answer directly", "body": "收到11月份"},
            },
        ))
        wf.process_event(ev2, workspace_id="ws-int-1", interaction_mode="direct")
        ctx = calls[-1]["context"]
        self.assertTrue(any(c.get("role") == "assistant" and "请提供月份" in c["content"] for c in ctx))
        caps2 = calls[-1]["available_capabilities"]
        self.assertEqual(caps2["tier"], "internal")
        self.assertIn("internal", caps2["shared_tools"])

    def test_multistep_plan_reuses_worker_session_id_and_enforces_ambient_silent_ack(self) -> None:
        self.manager.provision("ws-multi")
        decision = DssuperAgentDecision(
            decision_id="dec-multi",
            trigger_ids=["company-agent:ev-multi"],
            thread_key="company-agent:conv-multi",
            kind="ignore",
            outcome="ignore",
            reason="Two milestone phases needed",
            occurred_at="2026-11-19T12:00:00Z",
            strength="inferred",
            assistant_intent={
                "action": "propose_work",
                "rationale": "Gather then report silently",
                "title": "两阶段静默汇总",
                "objective": "Gather and compile report",
                "completion_conditions": ["Stage 1 done", "Stage 2 report done"],
                "plan_steps": [
                    {"title": "Stage 1", "objective": "Collect metrics", "capability": "dsh.task",
                     "completion_conditions": ["Metrics collected"]},
                    {"title": "Stage 2", "objective": "Write report", "capability": "dsh.task",
                     "completion_conditions": ["Final report written"]},
                ],
                "publication": {"recommendation": "group_update", "reason": "Deliver proactive summary"},
            },
        )
        upstream = FakeDssuperAgent(decision=decision)
        dispatched_params: list[dict[str, object]] = []

        class SessionTrackingDispatcher:
            knowledge_service = None

            def __init__(self, plane):
                self.plane = plane

            def dispatch(self, execution_id: str):
                req = self.plane.get_execution(execution_id)
                dispatched_params.append(dict(req.parameters))
                sid = f"worker-sess-{len(dispatched_params)}" if not req.parameters.get("resume_session_id") else str(req.parameters["resume_session_id"])
                return ExecutionResult(
                    execution_id, "succeeded", artifact_refs=list(req.parameters.get("expected_artifacts", [])),
                    metadata={"session_id": sid},
                )

        wf = DssuperAgentWorkflow(
            self.plane, upstream, manager=self.manager,
            dispatcher=SessionTrackingDispatcher(self.plane),
            policy=AssistantPolicy(auto_execute_capabilities=frozenset({"dsh.task"})),
        )
        ev = Event("ev-multi", "qq.group", "grp-m", "conv-multi", "u1", "2026-11-19T12:00:00Z", "群讨论汇总", [], ["dsh.task"])
        run = wf.process_event(ev, workspace_id="ws-multi", interaction_mode="ambient_cooldown")
        self.assertEqual(run.status, "completed")
        self.assertTrue(run.publication.get("silent_ack"))
        self.assertEqual(len(dispatched_params), 2)
        self.assertFalse(dispatched_params[0]["require_user_reply"])
        self.assertNotIn("resume_session_id", dispatched_params[0])
        self.assertTrue(dispatched_params[1]["require_user_reply"])
        self.assertEqual(dispatched_params[1]["resume_session_id"], "worker-sess-1")


class RecordingDispatcherForTest:
    knowledge_service = None

    def __init__(self, plane, manager):
        self.plane = plane
        self.manager = manager

    def dispatch(self, execution_id: str):
        return ExecutionResult(execution_id, "succeeded", artifact_refs=[])


if __name__ == "__main__":
    unittest.main()
