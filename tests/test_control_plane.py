from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.control_plane import ControlPlane, InvalidTransition
from app.fake_adapters import DeterministicAssistantAdapter
from app.models import Event, ExecutionResult, PlanStep


class ControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.plane = ControlPlane.open(Path(self.temp_dir.name) / "control.sqlite")

    def tearDown(self) -> None:
        self.plane.close()
        self.temp_dir.cleanup()

    def event(self, event_id: str = "event-1") -> Event:
        return Event(
            event_id=event_id,
            source_type="test",
            source_id="test-source",
            conversation_id="conversation-1",
            actor_id="actor-1",
            occurred_at="2026-11-08T00:00:00+00:00",
            content="A generic work request",
            attachments=[],
            capabilities=[],
        )

    def test_event_ingest_is_idempotent_and_audited_once(self) -> None:
        self.assertTrue(self.plane.ingest_event(self.event()))
        self.assertFalse(self.plane.ingest_event(self.event()))

        stored = self.plane.get_event("event-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.content, "A generic work request")
        audit = self.plane.list_audit(entity_type="event", entity_id="event-1")
        self.assertEqual([entry.action for entry in audit], ["ingested"])

    def test_work_item_plan_execution_and_result_form_a_complete_flow(self) -> None:
        self.plane.ingest_event(self.event())
        item = self.plane.create_work_item(
            work_item_id="work-1",
            title="Verify a work objective",
            objective="Produce a verifiable result",
            source_event_ids=["event-1"],
            completion_conditions=["result is stored"],
            risk_class="low",
            owner_ref=None,
        )
        self.assertEqual(item.status, "candidate")

        self.plane.propose_work_item("work-1")
        self.plane.accept_work_item("work-1")
        self.plane.queue_work_item("work-1")
        plan = self.plane.create_plan(
            plan_id="plan-1",
            work_item_id="work-1",
            steps=[PlanStep(step_id="step-1", ordinal=1, title="Run capability")],
        )
        self.assertEqual(plan.steps[0].status, "pending")

        execution = self.plane.create_execution_request(
            execution_id="execution-1",
            work_item_id="work-1",
            plan_step_id="step-1",
            workspace_id="workspace-1",
            capability="test.capability",
            parameters={"mode": "read-only"},
        )
        self.assertEqual(execution.status, "queued")
        self.plane.start_execution("execution-1")
        self.plane.record_execution_result(
            ExecutionResult(
                execution_id="execution-1",
                status="succeeded",
                output="verified",
                evidence_refs=["evidence-1"],
            )
        )

        saved = self.plane.get_work_item("work-1")
        self.assertEqual(saved.status, "completed")
        self.assertEqual(self.plane.get_plan("plan-1").steps[0].status, "completed")
        self.assertEqual(self.plane.get_execution("execution-1").status, "succeeded")

    def test_invalid_transition_is_rejected(self) -> None:
        self.plane.ingest_event(self.event())
        self.plane.create_work_item(
            work_item_id="work-1",
            title="Work",
            objective="Objective",
            source_event_ids=["event-1"],
            completion_conditions=["done"],
            risk_class="low",
            owner_ref=None,
        )

        with self.assertRaises(InvalidTransition):
            self.plane.start_work_item("work-1")

    def test_publication_decision_creates_one_idempotent_outbox_message(self) -> None:
        self.plane.ingest_event(self.event())
        self.plane.create_work_item(
            work_item_id="work-1",
            title="Work",
            objective="Objective",
            source_event_ids=["event-1"],
            completion_conditions=["done"],
            risk_class="low",
            owner_ref=None,
        )
        self.plane.propose_work_item("work-1")
        self.plane.accept_work_item("work-1")
        self.plane.queue_work_item("work-1")
        self.plane.start_work_item("work-1")
        self.plane.complete_work_item("work-1")

        first = self.plane.decide_publication(
            decision_id="decision-1",
            work_item_id="work-1",
            conversation_id="conversation-1",
            disposition="group_update",
            reason="The result is complete",
            policy_version="policy-1",
            body="The work completed with evidence.",
            idempotency_key="work-1:conversation-1:completed",
        )
        second = self.plane.decide_publication(
            decision_id="decision-2",
            work_item_id="work-1",
            conversation_id="conversation-1",
            disposition="group_update",
            reason="Duplicate publication attempt",
            policy_version="policy-1",
            body="The work completed with evidence.",
            idempotency_key="work-1:conversation-1:completed",
        )

        self.assertEqual(first.outbox_id, second.outbox_id)
        self.assertEqual(len(self.plane.list_outbox()), 1)
        self.assertEqual(self.plane.list_outbox()[0].status, "pending")

    def test_adapter_creates_a_proposed_item_and_context_records_are_persisted(self) -> None:
        conversation = self.plane.create_conversation(
            conversation_id="conversation-1",
            kind="test",
            participant_refs=["actor-1"],
            policy_version="policy-1",
        )
        thread = self.plane.create_thread(
            thread_id="thread-1", conversation_id=conversation.conversation_id, title="A thread"
        )
        self.plane.ingest_event(self.event())
        self.plane.attach_event_to_thread(thread.thread_id, "event-1")
        self.plane.save_memory(
            memory_id="memory-1",
            key="working-rule",
            value="keep evidence",
            source_event_ids=["event-1"],
            confidence=0.9,
        )
        proposed = DeterministicAssistantAdapter(self.plane).propose_work(
            event=self.event("event-2"),
            work_item_id="work-2",
            title="Proposed work",
            objective="Record a proposal",
        )

        self.assertEqual(self.plane.get_thread("thread-1").event_ids, ["event-1"])
        self.assertEqual(self.plane.get_memory("memory-1").confidence, 0.9)
        self.assertEqual(proposed.status, "proposed")

    def test_high_risk_execution_requires_approval_and_running_execution_recovers(self) -> None:
        self.plane.ingest_event(self.event())
        self.plane.create_work_item(
            work_item_id="work-1",
            title="Risky work",
            objective="Use an approved capability",
            source_event_ids=["event-1"],
            completion_conditions=["done"],
            risk_class="high",
            owner_ref="owner-1",
        )
        self.plane.propose_work_item("work-1")
        self.plane.accept_work_item("work-1")
        self.plane.queue_work_item("work-1")
        self.plane.create_plan(
            plan_id="plan-1",
            work_item_id="work-1",
            steps=[PlanStep(step_id="step-1", ordinal=1, title="Run", capability="safe.read")],
        )
        with self.assertRaises(InvalidTransition):
            self.plane.create_execution_request(
                execution_id="execution-1",
                work_item_id="work-1",
                plan_step_id="step-1",
                workspace_id="workspace-1",
                capability="safe.read",
                parameters={},
            )
        self.plane.create_execution_request(
            execution_id="execution-1",
            work_item_id="work-1",
            plan_step_id="step-1",
            workspace_id="workspace-1",
            capability="safe.read",
            parameters={},
            approval_ref="approval-1",
        )
        self.plane.start_execution("execution-1")
        self.plane.close()
        self.plane = ControlPlane.open(Path(self.temp_dir.name) / "control.sqlite")

        self.assertEqual(self.plane.get_execution("execution-1").status, "queued")
        self.assertEqual(self.plane.get_plan("plan-1").steps[0].status, "ready")
        self.assertEqual(self.plane.get_work_item("work-1").status, "queued")


if __name__ == "__main__":
    unittest.main()
