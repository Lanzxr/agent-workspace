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
from app.dssuperagent import DssuperAgentDecision, DssuperAgentProtocolError, DssuperAgentReceipt
from app.execution import ExecutionDispatcher
from app.models import Event
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

    def submit_event(self, event: Event, *, context: list[dict[str, object]] | None = None) -> DssuperAgentReceipt:
        self.submitted.append((event, context))
        return DssuperAgentReceipt("stored", f"company-agent:{event.event_id}",
                                   f"company-agent:{event.conversation_id}")

    def wait_for_decision(self, thread_key: str, trigger_id: str, *, timeout: float = 120) -> DssuperAgentDecision:
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

    def test_unlisted_capability_is_rejected_before_a_work_item_is_created(self) -> None:
        with self.assertRaises(DssuperAgentProtocolError):
            self.workflow().process_event(self.event(capabilities=[]))

        self.assertEqual(self.plane.store.connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], 0)
        self.assertEqual(self.plane.get_assistant_run("event-1").status, "failed")

    def test_auto_authorized_proposal_runs_in_workspace_and_returns_result_upstream_once(self) -> None:
        workflow = self.workflow(auto_execute=True)
        self.assertNotEqual(workflow.policy.version, AssistantPolicy().version)

        run = workflow.process_event(self.event(), workspace_id="assistant-workspace")
        repeated = workflow.process_event(self.event(), workspace_id="assistant-workspace")

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
        self.assertEqual(result.artifact_refs, ["artifacts/step-01-result.md"])
        self.assertEqual((self.manager.get("assistant-workspace").root / "work/launch-count.txt").read_text(), "run\n")
        self.assertEqual(len(self.upstream.results), 1)
        feedback = self.upstream.results[0]
        self.assertEqual(feedback.source_type, "company-agent.execution-result")
        self.assertEqual(json.loads(feedback.content)["status"], "succeeded")

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


if __name__ == "__main__":
    unittest.main()
