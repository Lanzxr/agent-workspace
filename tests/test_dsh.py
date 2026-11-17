from __future__ import annotations

import json
import shlex
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.control_plane import ControlPlane, InvalidTransition
from app.dsh import DshAdapter
from app.execution import ExecutionDispatcher
from app.execution_state import ExecutionBusy
from app.models import Event, PlanStep
from app.runtime_config import RuntimeConfig
from app.workspace import WorkspaceManager


class DshExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config_temporary_directory = tempfile.TemporaryDirectory()
        self.manager = WorkspaceManager(self.root / "workspaces")
        self.workspace = self.manager.provision("agent-1")
        self.plane = ControlPlane.open(self.root / "control.sqlite")
        self.config_file = Path(self.config_temporary_directory.name) / "runtime.env"
        self.config_file.write_text("test private runtime config", encoding="utf-8")
        self.config = RuntimeConfig("https://models.example/v1", "fixture-model", "test-secret-key",
                                    (sys.executable, str(Path(__file__).parent / "fixtures" / "dsh_stub.py")),
                                    self.config_file)
        self.bubblewrap = self.root / "bwrap"
        self.bubblewrap.write_text("test fixture", encoding="utf-8")
        self.bubblewrap.chmod(0o700)
        self.adapter = DshAdapter(self.config, self.manager, self.root / "runtime",
                                  bubblewrap_executable=self.bubblewrap)
        self.dispatcher = ExecutionDispatcher(self.plane, self.manager, self.adapter, self.root / "runtime")
        self.plane.ingest_event(Event("event-1", "test", "fixture", "conversation-1", "actor-1",
                                      "2026-11-15T00:00:00Z", "A generic task", [], ["dsh.task"]))

    def tearDown(self) -> None:
        self.plane.close()
        self.config_temporary_directory.cleanup()
        self.temporary_directory.cleanup()

    def create_request(self, prompt: str = "success", *, timeout: float = 10,
                       require_user_reply: bool = False) -> None:
        self.plane.create_work_item(work_item_id="work-1", title="Work", objective="Produce a file",
                                    source_event_ids=["event-1"], completion_conditions=["result.json exists"],
                                    risk_class="low", owner_ref=None)
        self.plane.propose_work_item("work-1")
        self.plane.accept_work_item("work-1")
        self.plane.queue_work_item("work-1")
        self.plane.create_plan(plan_id="plan-1", work_item_id="work-1",
                               steps=[PlanStep("step-1", 1, "Execute", capability="dsh.task")])
        self.plane.create_execution_request(execution_id="execution-1", work_item_id="work-1",
                                           plan_step_id="step-1", workspace_id="agent-1", capability="dsh.task",
                                           parameters={"prompt": prompt, "expected_artifacts": ["result.json"],
                                                       "timeout_seconds": timeout,
                                                       "require_user_reply": require_user_reply})

    def test_approved_execution_saves_tool_evidence_artifact_and_control_plane_result(self) -> None:
        self.create_request()

        result = self.dispatcher.dispatch("execution-1")

        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(self.plane.get_work_item("work-1").status, "completed")
        self.assertEqual(self.plane.get_execution_result("execution-1"), result)
        self.assertEqual(result.artifact_refs, ["artifacts/result.json"])
        self.assertEqual(result.metadata["session_id"], "fixture-session")
        self.assertEqual(result.metadata["tools_used"], ["bash"])
        self.assertEqual(json.loads((self.workspace.root / "artifacts/result.json").read_text()), {"total": 42})
        provider_patch = json.loads((self.root / "runtime" / "dsh" / "agent-1" / "provider.patch.yml").read_text())
        disabled = {entry["id"] for entry in provider_patch if entry.get("disabled") is True}
        self.assertTrue({"tool-fs", "tool-fs-search", "tool-bash-persistent", "tool-pwsh",
                         "tool-pwsh-persistent", "tool-terminal", "tool-str-replace-editor",
                         "tool-working-directory"}.issubset(disabled))
        self.assertEqual(result.metadata["shell_sandbox"], "bubblewrap-user-namespace")
        trace = (self.workspace.root / result.evidence_refs[0]).read_text(encoding="utf-8")
        self.assertIn('"tool_call"', trace)
        self.assertNotIn("test-secret-key", trace + result.output)
        self.assertNotIn("PRIVATE_REASONING", trace)

    def test_required_user_reply_block_is_validated_and_process_notes_are_excluded(self) -> None:
        self.create_request(require_user_reply=True)

        result = self.dispatcher.dispatch("execution-1")

        self.assertEqual(result.status, "succeeded", result.error)
        artifact = (self.workspace.root / "artifacts/result.json").read_text(encoding="utf-8")
        self.assertIn("<!-- COMPANY_AGENT_FINAL_REPLY_BEGIN:execution-1 -->", artifact)
        self.assertIn("Fixture final answer.", artifact)

    def test_missing_required_user_reply_block_fails_the_execution(self) -> None:
        self.create_request("missing-reply-contract", require_user_reply=True)

        result = self.dispatcher.dispatch("execution-1")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.metadata["error_category"], "reply_contract")
        self.assertEqual(self.plane.get_work_item("work-1").status, "failed")

    def test_duplicate_dispatch_returns_the_saved_result_without_repeating_tools(self) -> None:
        self.create_request()
        first = self.dispatcher.dispatch("execution-1")

        second = self.dispatcher.dispatch("execution-1")

        self.assertEqual(second, first)
        self.assertEqual((self.workspace.root / "work/launch-count.txt").read_text(), "run\n")

    def test_receipt_recovers_a_database_writeback_failure_after_process_restart(self) -> None:
        self.create_request()
        with patch.object(self.plane, "record_execution_result", side_effect=OSError("database unavailable")):
            with self.assertRaises(OSError):
                self.dispatcher.dispatch("execution-1")
        self.plane.close()
        self.plane = ControlPlane.open(self.root / "control.sqlite")
        restarted = ExecutionDispatcher(self.plane, self.manager, self.adapter, self.root / "runtime")

        result = restarted.dispatch("execution-1")

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.plane.get_work_item("work-1").status, "completed")
        self.assertEqual((self.workspace.root / "work/launch-count.txt").read_text(), "run\n")

    def test_interrupted_execution_without_a_receipt_fails_without_replaying_its_effects(self) -> None:
        self.create_request()
        self.plane.start_execution("execution-1")
        self.manager.mark_running("agent-1")
        self.plane.close()
        self.plane = ControlPlane.open(self.root / "control.sqlite")
        restarted = ExecutionDispatcher(self.plane, self.manager, self.adapter, self.root / "runtime")

        result = restarted.dispatch("execution-1")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.metadata["error_category"], "interrupted")
        self.assertFalse((self.workspace.root / "work/launch-count.txt").exists())
        self.assertEqual(self.manager.get("agent-1").status, "ready")

    def test_an_active_workspace_cannot_be_dispatched_or_restored_by_another_owner(self) -> None:
        self.create_request()
        self.plane.start_execution("execution-1")
        with self.manager.execution_lease("agent-1"):
            other_reader = ControlPlane.open(self.root / "control.sqlite")
            try:
                self.assertEqual(other_reader.get_execution("execution-1").status, "running")
            finally:
                other_reader.close()
            with self.assertRaises(ExecutionBusy):
                self.dispatcher.dispatch("execution-1")
            with self.assertRaises(ExecutionBusy):
                self.manager.restore("agent-1")

    def test_blocked_work_is_not_launched(self) -> None:
        self.create_request()
        self.plane.block_work_item("work-1", "Authorization withdrawn")
        with self.assertRaises(InvalidTransition):
            self.dispatcher.dispatch("execution-1")
        self.assertFalse((self.workspace.root / "work/launch-count.txt").exists())

    def test_missing_or_failed_protocol_and_missing_outputs_are_not_reported_as_success(self) -> None:
        for scenario, category in (("missing-final", "protocol_error"), ("bad-protocol", "protocol_error"),
                                   ("provider-error", "dsh_incomplete"), ("missing-artifact", "artifact_validation")):
            with self.subTest(scenario=scenario):
                self.create_request(scenario)
                result = self.dispatcher.dispatch("execution-1")
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.metadata["error_category"], category)
                self.assertEqual(self.plane.get_execution("execution-1").status, "failed")
                self.assertEqual(self.manager.get("agent-1").status, "ready")
            self.tearDown()
            self.setUp()

    def test_timeout_is_persisted_without_a_successful_task(self) -> None:
        self.create_request("timeout", timeout=0.1)
        result = self.dispatcher.dispatch("execution-1")
        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.metadata["error_category"], "timeout")
        self.assertEqual(self.plane.get_work_item("work-1").status, "failed")

    def test_existing_outputs_cannot_be_mistaken_for_new_work(self) -> None:
        self.create_request("missing-artifact")
        self.manager.save_artifact("agent-1", "result.json", '{"total": 99}')
        with self.assertRaises(ValueError):
            self.dispatcher.dispatch("execution-1")
        self.assertFalse((self.workspace.root / "work/launch-count.txt").exists())

    def test_artifact_paths_and_permission_overrides_are_rejected_before_launch(self) -> None:
        self.create_request()
        request = self.plane.get_execution("execution-1")
        for parameters in ({"prompt": "run", "expected_artifacts": ["../secret"]},
                           {"prompt": "run", "permission_mode": "danger-full-access"},
                           {"prompt": "run", "timeout_seconds": 301}):
            with self.subTest(parameters=parameters):
                request.parameters = parameters
                with self.assertRaises(ValueError):
                    self.adapter.preflight(request, self.workspace)

    def test_missing_bubblewrap_fails_preflight_before_execution_starts(self) -> None:
        self.create_request()
        self.adapter.bubblewrap_executable = self.root / "missing-bwrap"

        with self.assertRaisesRegex(ValueError, "Bubblewrap"):
            self.adapter.preflight(self.plane.get_execution("execution-1"), self.workspace)

        self.assertEqual(self.plane.get_execution("execution-1").status, "queued")

    def test_workspace_cannot_contain_company_agent_private_data_directory(self) -> None:
        self.create_request()
        unsafe_workspace = replace(self.workspace, root=self.manager.base_directory)

        with self.assertRaisesRegex(ValueError, "private runtime data"):
            self.adapter.preflight(self.plane.get_execution("execution-1"), unsafe_workspace)

        self.assertEqual(self.plane.get_execution("execution-1").status, "queued")

    def test_runtime_config_cannot_be_inside_the_selected_workspace(self) -> None:
        self.create_request()
        workspace_config = self.workspace.root / "work" / "runtime.env"
        workspace_config.write_text("test private runtime config", encoding="utf-8")
        self.adapter.config = replace(self.config, source_path=workspace_config)

        with self.assertRaisesRegex(ValueError, "runtime configuration cannot be inside"):
            self.adapter.preflight(self.plane.get_execution("execution-1"), self.workspace)

        self.assertEqual(self.plane.get_execution("execution-1").status, "queued")

    def test_approval_for_another_capability_does_not_authorize_dsh(self) -> None:
        self.plane.create_work_item(work_item_id="work-1", title="Restricted work", objective="Run tools",
                                    source_event_ids=["event-1"], completion_conditions=["done"],
                                    risk_class="high", owner_ref=None)
        self.plane.propose_work_item("work-1")
        self.plane.request_work_item_approval("work-1", "Review")
        self.plane.request_approval(approval_id="approval-1", work_item_id="work-1", scope="other.capability",
                                    policy_version="policy-1")
        self.plane.approve("approval-1", "reviewer-1")
        self.plane.accept_work_item("work-1")
        self.plane.queue_work_item("work-1")
        self.plane.create_plan(plan_id="plan-1", work_item_id="work-1", steps=[PlanStep("step-1", 1, "Run")])
        with self.assertRaises(InvalidTransition):
            self.plane.create_execution_request(execution_id="execution-1", work_item_id="work-1",
                                               plan_step_id="step-1", workspace_id="agent-1", capability="dsh.task",
                                               parameters={"prompt": "run"}, approval_ref="approval-1")

    def test_runtime_provider_patch_adds_file_hiding_to_bwrap_profile(self) -> None:
        self.create_request()
        self.plane.start_execution("execution-1")
        self.manager.mark_running("agent-1")

        runtime_home = self.root / "runtime" / "dsh" / "agent-1"
        runtime_home.mkdir(parents=True)
        sandbox_home = self.root.parent / f"{self.root.name}-sandbox-home"
        sandbox_workspace = self.workspace.root
        with patch("app.dsh.Path.home", return_value=sandbox_home):
            runner = self.adapter._write_sandbox_runner(runtime_home, sandbox_workspace)
        contents = runner.read_text(encoding="utf-8")

        self.assertIn("--tmpfs /mnt", contents)
        self.assertIn("--tmpfs /run", contents)
        self.assertIn("--unshare-net", contents)
        self.assertIn(f"--tmpfs {shlex.quote(str(self.root))}", contents)
        self.assertIn(f"--tmpfs {shlex.quote(str(sandbox_home))}", contents)
        self.assertIn(f"--bind {shlex.quote(str(sandbox_workspace))}", contents)
        self.assertIn(f"--ro-bind /dev/null {shlex.quote(str(self.config_file))}", contents)


if __name__ == "__main__":
    unittest.main()
