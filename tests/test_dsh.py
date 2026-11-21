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
from app.models import Event, ExecutionRequest, PlanStep
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
                       idle_timeout: float | None = None, compaction_timeout: float | None = None,
                       soft_wrapup: float | None = None,
                       require_user_reply: bool = False) -> None:
        self.plane.create_work_item(work_item_id="work-1", title="Work", objective="Produce a file",
                                    source_event_ids=["event-1"], completion_conditions=["result.json exists"],
                                    risk_class="low", owner_ref=None)
        self.plane.propose_work_item("work-1")
        self.plane.accept_work_item("work-1")
        self.plane.queue_work_item("work-1")
        self.plane.create_plan(plan_id="plan-1", work_item_id="work-1",
                               steps=[PlanStep("step-1", 1, "Execute", capability="dsh.task")])
        parameters: dict[str, object] = {
            "prompt": prompt,
            "expected_artifacts": ["result.json"],
            "timeout_seconds": timeout,
            "require_user_reply": require_user_reply,
        }
        if idle_timeout is not None:
            parameters["idle_timeout_seconds"] = idle_timeout
        if compaction_timeout is not None:
            parameters["compaction_timeout_seconds"] = compaction_timeout
        if soft_wrapup is not None:
            parameters["soft_wrapup_seconds"] = soft_wrapup
        self.plane.create_execution_request(execution_id="execution-1", work_item_id="work-1",
                                           plan_step_id="step-1", workspace_id="agent-1", capability="dsh.task",
                                           parameters=parameters)

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
                           {"prompt": "run", "timeout_seconds": 7201},
                           {"prompt": "run", "idle_timeout_seconds": 7201},
                           {"prompt": "run", "soft_wrapup_seconds": 601}):
            with self.subTest(parameters=parameters):
                request.parameters = parameters
                with self.assertRaises(ValueError):
                    self.adapter.preflight(request, self.workspace)

        request.parameters = {"prompt": "run", "timeout_seconds": 7200}
        self.assertEqual(self.adapter.preflight(request, self.workspace).timeout_seconds, 7200.0)

    def test_active_stream_extends_beyond_idle_timeout_and_writes_live_trace(self) -> None:
        self.create_request("active-stream", timeout=5.0, idle_timeout=0.35)
        result = self.dispatcher.dispatch("execution-1")
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(result.metadata["limits"]["idle_timeout_seconds"], 0.35)

    def test_live_trace_is_flushed_while_dsh_is_still_running(self) -> None:
        self.create_request("live-trace-check", timeout=5.0)
        result = self.dispatcher.dispatch("execution-1")
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual((self.workspace.root / "work/live-trace-saw-call.txt").read_text(encoding="utf-8"), "yes")

    def test_idle_hang_without_progress_times_out_promptly(self) -> None:
        self.create_request("idle-hang-early", timeout=5.0, idle_timeout=0.25, soft_wrapup=2.0)
        result = self.dispatcher.dispatch("execution-1")
        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.metadata["error_category"], "idle_timeout")
        self.assertEqual((self.workspace.root / "work/launch-count.txt").read_text(encoding="utf-8"), "run\n")

    def test_soft_wrapup_on_approaching_total_timeout_resumes_session_and_delivers(self) -> None:
        self.adapter.web_search_enabled = lambda: True
        self.create_request("soft-wrapup", timeout=4.0, idle_timeout=3.0, soft_wrapup=2.0, require_user_reply=True)
        result = self.dispatcher.dispatch("execution-1")
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual((self.workspace.root / "work/launch-count.txt").read_text(encoding="utf-8"), "run\nrun\n")
        self.assertTrue(result.metadata["soft_wrapup"]["triggered"])
        self.assertEqual(result.metadata["soft_wrapup"]["reason"], "soft_timeout")
        self.assertTrue(result.metadata["soft_wrapup"]["succeeded"])
        meta = json.loads((self.workspace.root / "work/wrapup-meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["session_id"], "fixture-session")
        patch_by_id = {entry["id"]: entry for entry in meta["patch"]}
        self.assertTrue(patch_by_id["tool-web"]["disabled"])
        self.assertIn("超时前软收尾", meta["prompt"])
        artifact = (self.workspace.root / "artifacts/result.json").read_text(encoding="utf-8")
        self.assertIn("collected phase1 evidence", artifact)
        trace = (self.workspace.root / result.evidence_refs[0]).read_text(encoding="utf-8")
        self.assertIn("call-p1", trace)
        self.assertIn("call-1", trace)

    def test_soft_wrapup_on_idle_after_progress_recovers_staged_work(self) -> None:
        self.create_request("idle-hang-with-progress", timeout=5.0, idle_timeout=0.25, soft_wrapup=2.0)
        result = self.dispatcher.dispatch("execution-1")
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(result.metadata["soft_wrapup"]["reason"], "idle_timeout")
        self.assertEqual(json.loads((self.workspace.root / "artifacts/result.json").read_text(encoding="utf-8")),
                         {"partial": "collected phase1 evidence"})

    def test_failed_soft_wrapup_returns_timed_out(self) -> None:
        self.create_request("soft-wrapup-fail", timeout=2.0, idle_timeout=0.2, soft_wrapup=0.4)
        result = self.dispatcher.dispatch("execution-1")
        self.assertEqual(result.status, "timed_out")
        self.assertTrue(result.metadata["soft_wrapup"]["triggered"])
        self.assertFalse(result.metadata["soft_wrapup"]["succeeded"])

    def test_compaction_in_session_log_is_recognized_and_exempt_from_short_idle_timeout(self) -> None:
        self.create_request("compaction-ok", timeout=6.0, idle_timeout=0.2, compaction_timeout=2.0, soft_wrapup=0.0)
        result = self.dispatcher.dispatch("execution-1")
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(result.metadata["limits"]["compaction_timeout_seconds"], 2.0)
        trace = (self.workspace.root / result.evidence_refs[0]).read_text(encoding="utf-8")
        self.assertIn('"phase": "compaction_start"', trace)
        self.assertIn('"phase": "compaction_end"', trace)

    def test_exceeding_compaction_timeout_reports_compaction_timeout(self) -> None:
        self.create_request("compaction-hang", timeout=5.0, idle_timeout=0.2, compaction_timeout=0.35, soft_wrapup=0.0)
        result = self.dispatcher.dispatch("execution-1")
        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.metadata["error_category"], "compaction_timeout")

    def test_soft_wrapup_recovers_verified_artifact_if_post_write_compaction_times_out(self) -> None:
        self.create_request(
            "wrapup-writes-then-compacts",
            timeout=5.0,
            idle_timeout=0.2,
            compaction_timeout=0.3,
            soft_wrapup=1.5,
            require_user_reply=True,
        )
        result = self.dispatcher.dispatch("execution-1")
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertTrue(result.metadata["soft_wrapup"]["triggered"])
        self.assertTrue(result.metadata["soft_wrapup"]["succeeded"])
        self.assertEqual(result.metadata["soft_wrapup"]["recovered_from_category"], "compaction_timeout")
        artifact = (self.workspace.root / "artifacts/result.json").read_text(encoding="utf-8")
        self.assertIn("阶段性整理结果", artifact)

    def test_execution_contract_guides_network_tools_based_on_web_search_setting(self) -> None:
        request = ExecutionRequest(
            execution_id="exec-contract",
            work_item_id="work-1",
            plan_step_id="step-1",
            workspace_id="agent-1",
            capability="dsh.task",
            parameters={"prompt": "collect latest news"},
            status="running",
            created_at="2026-11-18T00:00:00Z",
            updated_at="2026-11-18T00:00:00Z",
        )
        task = self.adapter.preflight(request, self.workspace)
        enabled_contract = self.adapter._build_execution_contract(request, task, web_enabled=True)
        disabled_contract = self.adapter._build_execution_contract(request, task, web_enabled=False)
        self.assertIn("network-isolated", enabled_contract)
        self.assertIn("never run curl, wget", enabled_contract)
        self.assertIn("web_fetch and web_search", enabled_contract)
        self.assertIn("web tools are disabled", disabled_contract)

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

    def test_searxng_plugin_patch_unsetenv_and_shared_tools_isolation(self) -> None:
        from app.shared_tools import SharedToolsManager
        tools = SharedToolsManager(self.root / "shared-tools")
        tools.ensure_layers()
        tools.save_script("common", "common-tool", "#!/bin/sh\necho common\n")
        tools.save_script("internal", "internal-tool", "#!/bin/sh\necho internal\n")

        self.adapter.config = replace(
            self.config,
            web_search_enabled=True,
            web_gate_search_url="http://127.0.0.1:8767/internal/web/dsh-search",
            web_gate_api_key="super-secret-gate-key",
        )
        runtime_home = self.root / "runtime" / "dsh" / "agent-tools"
        runtime_home.mkdir(parents=True)
        patch_list = self.adapter._build_provider_patch(runtime_home / "runner", web_enabled=True)
        patch_text = json.dumps(patch_list)
        self.assertNotIn("super-secret-gate-key", patch_text)
        patch_by_id = {item["id"]: item for item in patch_list if "id" in item}
        self.assertEqual(patch_by_id["web"]["config"]["searchProvider"], "company-agent-searxng")
        self.assertTrue(patch_by_id["tool-web"]["config"]["search"])
        insert_entries = [item["insert"] for item in patch_list if "insert" in item]
        self.assertTrue(any(e[0]["id"] == "company-agent-searxng" for e in insert_entries))

        client_runner = self.adapter._write_sandbox_runner(
            runtime_home, self.workspace.root, workspace_tier="client_group"
        ).read_text(encoding="utf-8")
        internal_runner = self.adapter._write_sandbox_runner(
            runtime_home, self.workspace.root, workspace_tier="internal"
        ).read_text(encoding="utf-8")
        common_path = str((self.root / "shared-tools" / "common").resolve())
        internal_path = str((self.root / "shared-tools" / "internal").resolve())
        self.assertIn("--unsetenv COMPANY_AGENT_MODEL_API_KEY", client_runner)
        self.assertIn("--unsetenv COMPANY_AGENT_WEB_GATE_API_KEY", client_runner)
        self.assertIn(f"--ro-bind {shlex.quote(common_path)}", client_runner)
        self.assertNotIn(internal_path, client_runner)
        self.assertIn(f"--ro-bind {shlex.quote(common_path)}", internal_runner)
        self.assertIn(f"--ro-bind {shlex.quote(internal_path)}", internal_runner)

        env_client = {"PATH": "/usr/bin"}
        self.adapter._apply_runtime_environment(env_client, web_enabled=True, workspace_tier="client_group")
        self.assertIn(str((self.root / "shared-tools" / "common" / "bin").resolve()), env_client["PATH"])
        self.assertNotIn("shared-tools/internal", env_client["PATH"].replace("\\", "/"))
        self.assertEqual(env_client["COMPANY_AGENT_WEB_GATE_API_KEY"], "super-secret-gate-key")

        env_internal = {"PATH": "/usr/bin"}
        self.adapter._apply_runtime_environment(env_internal, web_enabled=False, workspace_tier="internal")
        self.assertIn(str((self.root / "shared-tools" / "internal" / "bin").resolve()), env_internal["PATH"])
        self.assertNotIn("COMPANY_AGENT_WEB_GATE_API_KEY", env_internal)

    def test_execution_contract_avoids_probing_when_no_inputs_and_allows_explicit_prior_paths(self) -> None:
        base_request = ExecutionRequest(
            execution_id="exec-no-probe", work_item_id="work-1", plan_step_id="step-1",
            workspace_id="agent-1", capability="dsh.task",
            parameters={"prompt": "search today's AI news"}, status="running",
            created_at="2026-11-18T00:00:00Z", updated_at="2026-11-18T00:00:00Z",
        )
        task_no_inputs = self.adapter.preflight(base_request, self.workspace)
        no_probe_contract = self.adapter._build_execution_contract(base_request, task_no_inputs, web_enabled=True)
        self.assertIn("do NOT run ls/find to probe", no_probe_contract)

        prior_request = replace(
            base_request,
            parameters={"prompt": "summarize\nPrior-step artifact path(s) (read ONLY these paths if needed):\n- artifacts/exec-1/step-01-result.md"},
        )
        task_with_prior = self.adapter.preflight(prior_request, self.workspace)
        prior_contract = self.adapter._build_execution_contract(prior_request, task_with_prior, web_enabled=True)
        self.assertIn("Read ONLY the explicit input/prior-step paths", prior_contract)
        self.assertNotIn("do NOT run ls/find to probe", prior_contract)

    def test_resume_session_id_is_forwarded_and_falls_back_cleanly_on_startup_failure(self) -> None:
        self.plane.create_work_item(work_item_id="work-1", title="Work", objective="Multi-step resume",
                                    source_event_ids=["event-1"], completion_conditions=["result.json exists"],
                                    risk_class="low", owner_ref=None)
        self.plane.propose_work_item("work-1")
        self.plane.accept_work_item("work-1")
        self.plane.queue_work_item("work-1")
        self.plane.create_plan(plan_id="plan-1", work_item_id="work-1", steps=[
            PlanStep("step-1", 1, "Stage 1", capability="dsh.task"),
            PlanStep("step-2", 2, "Stage 2", capability="dsh.task"),
        ])

        self.plane.create_execution_request(
            execution_id="exec-resume-ok", work_item_id="work-1", plan_step_id="step-1",
            workspace_id="agent-1", capability="dsh.task",
            parameters={
                "prompt": "success",
                "expected_artifacts": ["step1.json"],
                "timeout_seconds": 10,
                "require_user_reply": False,
                "resume_session_id": "prev-session-9",
            },
        )
        ok_res = self.dispatcher.dispatch("exec-resume-ok")
        self.assertEqual(ok_res.status, "succeeded", ok_res.error)
        self.assertEqual(ok_res.metadata.get("session_id"), "prev-session-9")
        self.assertTrue(ok_res.metadata.get("session_resumed"))

        self.plane.create_execution_request(
            execution_id="exec-resume-fallback", work_item_id="work-1", plan_step_id="step-2",
            workspace_id="agent-1", capability="dsh.task",
            parameters={
                "prompt": "broken-resume",
                "expected_artifacts": ["step2.json"],
                "timeout_seconds": 10,
                "require_user_reply": False,
                "resume_session_id": "stale-session-1",
            },
        )
        fallback_res = self.dispatcher.dispatch("exec-resume-fallback")
        self.assertEqual(fallback_res.status, "succeeded", fallback_res.error)
        self.assertEqual(fallback_res.metadata.get("session_id"), "fixture-session")
        self.assertFalse(fallback_res.metadata.get("session_resumed"))
        launches = (self.workspace.directory("work") / "launch-count.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(launches), 3)

    def test_contract_wrapup_recovers_malformed_dsml_missing_artifact(self) -> None:
        self.create_request("malformed-dsml-missing-artifact", timeout=10.0)
        result = self.dispatcher.dispatch("execution-1")

        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(result.artifact_refs, ["artifacts/result.json"])
        wrapup = result.metadata["soft_wrapup"]
        self.assertTrue(wrapup["triggered"])
        self.assertEqual(wrapup["reason"], "contract_artifact_validation")
        self.assertTrue(wrapup["model_markup_leak"])
        self.assertTrue(wrapup["succeeded"])
        self.assertNotIn("DSML", result.output)
        saved = json.loads((self.workspace.root / "artifacts/result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["partial"], "collected workbook output")

    def test_contract_wrapup_recovers_missing_artifact_and_reply_under_standard_budget(self) -> None:
        self.create_request("contract-missing-artifact-ok", timeout=30.0, soft_wrapup=2.0)
        res_art = self.dispatcher.dispatch("execution-1")
        self.assertEqual(res_art.status, "succeeded", res_art.error)
        self.assertEqual(res_art.metadata["soft_wrapup"]["reason"], "contract_artifact_validation")
        self.assertTrue(res_art.metadata["soft_wrapup"]["succeeded"])

        self.tearDown()
        self.setUp()
        self.create_request("contract-missing-reply-ok", timeout=30.0, soft_wrapup=2.0, require_user_reply=True)
        res_rep = self.dispatcher.dispatch("execution-1")
        self.assertEqual(res_rep.status, "succeeded", res_rep.error)
        self.assertEqual(res_rep.metadata["soft_wrapup"]["reason"], "contract_reply_contract")
        self.assertTrue(res_rep.metadata["soft_wrapup"]["succeeded"])
        artifact = (self.workspace.root / "artifacts/result.json").read_text(encoding="utf-8")
        self.assertIn("<!-- COMPANY_AGENT_FINAL_REPLY_BEGIN:execution-1 -->", artifact)

    def test_contract_wrapup_failure_still_reports_artifact_validation(self) -> None:
        self.create_request("contract-missing-artifact-fail", timeout=30.0, soft_wrapup=2.0)
        result = self.dispatcher.dispatch("execution-1")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.metadata["error_category"], "artifact_validation")
        self.assertTrue(result.metadata["soft_wrapup"]["triggered"])
        self.assertFalse(result.metadata["soft_wrapup"]["succeeded"])


if __name__ == "__main__":
    unittest.main()
