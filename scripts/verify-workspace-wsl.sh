#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}" python3 -m unittest tests.test_workspace -v

workspace_root="$(mktemp -d -t company-agent-workspace-XXXXXX)"
trap 'rm -rf "$workspace_root"' EXIT

PYTHONPATH="$repo_root" WORKSPACE_ROOT="$workspace_root" python3 - <<'PY'
import os
from pathlib import Path

from app.workspace import WorkspaceManager
from app.workspace_runtime import WorkspaceRuntime

manager = WorkspaceManager(Path(os.environ["WORKSPACE_ROOT"]))
manager.provision("process-restart")
execution = WorkspaceRuntime(manager).run_python(
    "process-restart",
    "from pathlib import Path; Path('artifacts/restart.txt').write_text('restart-ok')",
    cwd=".",
    execution_id="restart-1",
)
assert execution.status == "succeeded", execution.status
PY

PYTHONPATH="$repo_root" WORKSPACE_ROOT="$workspace_root" python3 - <<'PY'
import os
from pathlib import Path

from app.workspace import WorkspaceManager
from app.workspace_runtime import WorkspaceRuntime

root = Path(os.environ["WORKSPACE_ROOT"])
manager = WorkspaceManager(root)
execution = WorkspaceRuntime(manager).get_execution("process-restart", "restart-1")
assert execution.status == "succeeded", execution.status
assert (root / "process-restart" / "artifacts" / "restart.txt").read_text() == "restart-ok"
print("WSL_PROCESS_RESTART_OK")
PY
