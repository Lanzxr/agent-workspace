from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from app.workspace import WorkspaceManager, WorkspacePathError, WorkspaceStateError
from app.workspace_runtime import WorkspaceLimits, WorkspaceRuntime


class WorkspaceManagerTests(unittest.TestCase):
    def test_provision_creates_the_workspace_layout_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = WorkspaceManager(Path(temporary_directory))

            workspace = manager.provision("agent-1")

            self.assertEqual(workspace.status, "ready")
            for directory in ("work", "tools", "artifacts", "logs", "state", "cache"):
                self.assertTrue((workspace.root / directory).is_dir())
            self.assertTrue((workspace.root / "state" / "workspace.json").is_file())

    def test_restore_returns_a_running_workspace_to_ready_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_directory = Path(temporary_directory)
            first_manager = WorkspaceManager(base_directory)
            first_manager.provision("agent-1")
            first_manager.mark_running("agent-1")

            restored = WorkspaceManager(base_directory).restore("agent-1")

            self.assertEqual(restored.status, "ready")
            self.assertEqual(WorkspaceManager(base_directory).summary("agent-1")["status"], "ready")

    def test_artifact_and_log_paths_cannot_escape_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = WorkspaceManager(Path(temporary_directory))
            manager.provision("agent-1")

            artifact = manager.save_artifact("agent-1", "report/result.txt", "done")
            manager.append_log("agent-1", "run.log", "completed\n")

            self.assertEqual(artifact.read_text(encoding="utf-8"), "done")
            self.assertEqual((artifact.parents[2] / "logs" / "run.log").read_text(encoding="utf-8"), "completed\n")
            with self.assertRaises(WorkspacePathError):
                manager.save_artifact("agent-1", "../outside.txt", "blocked")

    def test_symlinked_paths_cannot_escape_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_directory = Path(temporary_directory)
            manager = WorkspaceManager(base_directory)
            workspace = manager.provision("agent-1")
            outside = base_directory / "outside"
            outside.mkdir()
            link = workspace.root / "work" / "outside-link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symbolic links are unavailable in this environment")

            with self.assertRaises(WorkspacePathError):
                manager.validate_path("agent-1", Path("work/outside-link/file.txt"))

    def test_top_level_symlink_is_rejected_during_layout_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_directory = Path(temporary_directory)
            manager = WorkspaceManager(base_directory)
            workspace = manager.provision("agent-1")
            outside = base_directory / "outside"
            outside.mkdir()
            artifacts = workspace.root / "artifacts"
            artifacts.rmdir()
            try:
                artifacts.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symbolic links are unavailable in this environment")

            with self.assertRaises(WorkspacePathError):
                manager.provision("agent-1")

    def test_suspended_and_deleted_workspaces_reject_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = WorkspaceManager(Path(temporary_directory))
            manager.provision("agent-1")
            manager.mark_suspended("agent-1")

            with self.assertRaises(WorkspaceStateError):
                manager.save_input("agent-1", "request.txt", "blocked")

            manager = WorkspaceManager(Path(temporary_directory))
            manager.mark_deleted("agent-1")
            with self.assertRaises(WorkspaceStateError):
                manager.save_artifact("agent-1", "result.txt", "blocked")

    @unittest.skipUnless(os.name != "nt", "POSIX permissions are validated in WSL/Linux")
    def test_linux_workspace_permissions_are_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = WorkspaceManager(Path(temporary_directory)).provision("agent-1")

            self.assertEqual(stat.S_IMODE(workspace.root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((workspace.root / "state" / "workspace.json").stat().st_mode), 0o600)

    def test_python_execution_persists_logs_and_can_be_read_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_directory = Path(temporary_directory)
            manager = WorkspaceManager(base_directory)
            manager.provision("agent-1")
            runtime = WorkspaceRuntime(manager)

            execution = runtime.run_python(
                "agent-1",
                "from pathlib import Path; Path('artifacts/result.txt').write_text('workspace-ok'); print('runtime-ok')",
                cwd=".",
                execution_id="python-1",
            )

            restarted_runtime = WorkspaceRuntime(WorkspaceManager(base_directory))
            self.assertEqual(execution.status, "succeeded")
            self.assertEqual(restarted_runtime.get_execution("agent-1", "python-1").status, "succeeded")
            self.assertEqual(restarted_runtime.read_logs("agent-1", "python-1")["stdout"].strip(), "runtime-ok")
            self.assertEqual((base_directory / "agent-1" / "artifacts" / "result.txt").read_text(), "workspace-ok")

    @unittest.skipUnless(os.name != "nt", "POSIX shell execution is validated in WSL/Linux")
    def test_shell_execution_runs_in_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = WorkspaceManager(Path(temporary_directory))
            manager.provision("agent-1")

            execution = WorkspaceRuntime(manager).run_shell("agent-1", "printf shell-ok", execution_id="shell-1")

            self.assertEqual(execution.status, "succeeded")
            self.assertEqual(WorkspaceRuntime(manager).read_logs("agent-1", "shell-1")["stdout"], "shell-ok")

    def test_timeout_is_recorded_and_workspace_returns_to_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_directory = Path(temporary_directory)
            manager = WorkspaceManager(base_directory)
            manager.provision("agent-1")
            runtime = WorkspaceRuntime(manager, WorkspaceLimits(timeout_seconds=0.1, max_output_bytes=128))

            execution = runtime.run_python("agent-1", "import time; time.sleep(1)", execution_id="timeout-1")

            self.assertEqual(execution.status, "timed_out")
            self.assertEqual(manager.get("agent-1").status, "ready")

    def test_output_limit_is_recorded_without_losing_execution_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = WorkspaceManager(Path(temporary_directory))
            manager.provision("agent-1")
            runtime = WorkspaceRuntime(manager, WorkspaceLimits(timeout_seconds=5, max_output_bytes=32))

            execution = runtime.run_python("agent-1", "print('x' * 1000)", execution_id="output-limit-1")

            self.assertEqual(execution.status, "succeeded")
            self.assertTrue(execution.stdout_truncated)
            self.assertEqual(len(runtime.read_logs("agent-1", "output-limit-1")["stdout"].encode()), 32)

    def test_execution_ids_are_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = WorkspaceManager(Path(temporary_directory))
            manager.provision("agent-1")
            runtime = WorkspaceRuntime(manager)
            runtime.run_python("agent-1", "print('first')", execution_id="stable-1")

            with self.assertRaises(WorkspaceStateError):
                runtime.run_python("agent-1", "print('second')", execution_id="stable-1")

    def test_dependencies_preflight_and_interrupted_execution_are_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_directory = Path(temporary_directory)
            manager = WorkspaceManager(base_directory)
            manager.provision("agent-1")
            runtime = WorkspaceRuntime(manager)
            preflight = runtime.preflight("agent-1")
            manager.record_dependency("agent-1", "python", "3.14", "system")
            manager.mark_running("agent-1")
            manager.save_execution_record("agent-1", {"execution_id": "crash-1", "status": "running"})

            restored = WorkspaceManager(base_directory).restore("agent-1")

            self.assertEqual(preflight["status"], "ready")
            self.assertEqual(restored.status, "ready")
            self.assertEqual(WorkspaceManager(base_directory).get_execution_record("agent-1", "crash-1")["status"], "interrupted")
            self.assertEqual(WorkspaceManager(base_directory).list_dependencies("agent-1")[0]["name"], "python")


if __name__ == "__main__":
    unittest.main()
