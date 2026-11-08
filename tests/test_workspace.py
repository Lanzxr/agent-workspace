from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from app.workspace import WorkspaceManager, WorkspacePathError


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

    @unittest.skipUnless(os.name != "nt", "POSIX permissions are validated in WSL/Linux")
    def test_linux_workspace_permissions_are_restricted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = WorkspaceManager(Path(temporary_directory)).provision("agent-1")

            self.assertEqual(stat.S_IMODE(workspace.root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((workspace.root / "state" / "workspace.json").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
