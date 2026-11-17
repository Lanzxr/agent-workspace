from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shlex
import shutil
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .execution_state import private_directory, write_private_json
from .models import ExecutionRequest, ExecutionResult
from .reply_contract import extract_final_reply, final_reply_markers
from .process_runner import run_process
from .runtime_config import DSH_VERSION, POLICY_VERSION, RuntimeConfig
from .workspace import Workspace, WorkspaceManager


@dataclass(frozen=True)
class DshTask:
    prompt: str
    expected_artifacts: tuple[str, ...]
    timeout_seconds: float = 240
    max_output_bytes: int = 2_000_000
    max_artifact_bytes: int = 16_000_000
    knowledge_document_ids: tuple[str, ...] = ()
    attachment_ids: tuple[str, ...] = ()
    require_user_reply: bool = False

    @classmethod
    def from_request(cls, request: ExecutionRequest) -> "DshTask":
        if request.capability != "dsh.task":
            raise ValueError("DSH Adapter only accepts dsh.task")
        parameters = request.parameters
        if set(parameters) - {"prompt", "expected_artifacts", "timeout_seconds", "knowledge_document_ids",
                              "attachment_ids",
                              "require_user_reply"}:
            raise ValueError("unsupported dsh.task parameters")
        prompt = parameters.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 64000:
            raise ValueError("prompt must contain 1-64000 UTF-8 bytes")
        artifacts = parameters.get("expected_artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) > 100:
            raise ValueError("expected_artifacts must be a list of at most 100 paths")
        for name in artifacts:
            if (not isinstance(name, str) or not name or "\\" in name or ":" in name
                    or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/"))):
                raise ValueError("artifact paths must be relative paths inside artifacts/")
        if len(set(artifacts)) != len(artifacts):
            raise ValueError("expected artifact paths must be unique")
        timeout = parameters.get("timeout_seconds", 240)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 300):
            raise ValueError("timeout_seconds must be in (0, 300]")
        document_ids = parameters.get("knowledge_document_ids", [])
        if (not isinstance(document_ids, list) or len(document_ids) > 20
                or any(not isinstance(value, str) or not value or len(value) > 200 for value in document_ids)
                or len(set(document_ids)) != len(document_ids)):
            raise ValueError("knowledge_document_ids must contain at most twenty unique IDs")
        attachment_ids = parameters.get("attachment_ids", [])
        if (not isinstance(attachment_ids, list) or len(attachment_ids) > 20
                or any(not isinstance(value, str) or not value or len(value) > 200 for value in attachment_ids)
                or len(set(attachment_ids)) != len(attachment_ids)):
            raise ValueError("attachment_ids must contain at most twenty unique IDs")
        if (document_ids or attachment_ids) and len(prompt.encode("utf-8")) > 63_000:
            raise ValueError("prompt leaves too little space for attached-file references")
        require_user_reply = parameters.get("require_user_reply", False)
        if not isinstance(require_user_reply, bool):
            raise ValueError("require_user_reply must be a boolean")
        return cls(prompt, tuple(artifacts), float(timeout), knowledge_document_ids=tuple(document_ids),
                   attachment_ids=tuple(attachment_ids),
                   require_user_reply=require_user_reply)


class DshAdapter:
    """Drive the official headless transport; the control plane owns task state."""

    def __init__(self, config: RuntimeConfig, manager: WorkspaceManager, runtime_directory: Path,
                 *, bubblewrap_executable: Path | None = None,
                 web_search_enabled: Callable[[], bool] | None = None) -> None:
        self.config = config
        self.manager = manager
        self.runtime_directory = private_directory(runtime_directory.resolve())
        self.private_data_directory = self.runtime_directory.parent
        self.bubblewrap_executable = bubblewrap_executable
        self.web_search_enabled = web_search_enabled
        if not manager.base_directory.is_relative_to(self.private_data_directory):
            raise ValueError("Workspace and DSH runtime data must share one private data directory")
        if self.runtime_directory.is_relative_to(manager.base_directory):
            raise ValueError("DSH configuration and receipts must be outside Agent workspaces")

    def preflight(self, request: ExecutionRequest, workspace: Workspace) -> DshTask:
        task = DshTask.from_request(request)
        self.config.resolve_command()
        self._resolve_bubblewrap()
        workspace_root = workspace.root.resolve()
        private_data_root = self.private_data_directory.resolve()
        workspace_base = self.manager.base_directory.resolve()
        if (workspace_root in {private_data_root, workspace_base}
                or private_data_root.is_relative_to(workspace_root)
                or workspace_base.is_relative_to(workspace_root)):
            raise ValueError("Agent Workspace cannot contain private runtime data or other Workspaces")
        if self.config.source_path is not None and self.config.source_path.expanduser().resolve().is_relative_to(workspace_root):
            raise ValueError("runtime configuration cannot be inside an Agent Workspace")
        if os.name != "nt":
            host_home = Path.home().resolve()
            hidden_roots = (Path("/mnt"), Path("/run"), Path("/tmp"))
            if any(workspace_root.is_relative_to(root) for root in hidden_roots):
                raise ValueError("Agent Workspace must be outside /mnt, /run and /tmp for DSH Shell isolation")
            if private_data_root == Path("/") or any(private_data_root.is_relative_to(root) for root in hidden_roots):
                raise ValueError("Company Agent private data must be outside /mnt, /run and /tmp")
            required_system_paths = (Path("/bin"), Path("/dev"), Path("/etc"), Path("/proc"),
                                     Path("/sys"), Path("/usr"))
            if any(path.is_relative_to(private_data_root) for path in required_system_paths):
                raise ValueError("Company Agent private data cannot contain Linux system directories")
            if workspace_root == host_home or host_home.is_relative_to(workspace_root):
                raise ValueError("Agent Workspace cannot contain the WSL user home")
            protected_paths = (".config", ".ssh", ".aws", ".azure", ".gcloud", ".kube", ".docker",
                               ".gnupg", ".local", ".local/share/company-agent", ".git-credentials",
                               ".netrc", ".npmrc", ".pypirc")
            if any((host_home / path).is_relative_to(workspace_root) for path in protected_paths):
                raise ValueError("Agent Workspace cannot contain WSL user configuration or runtime data")
        for name in task.expected_artifacts:
            path = self._artifact_path(workspace, name)
            if path.exists():
                raise ValueError(f"expected artifact already exists; choose a new output path: {name}")
        return task

    def execute(self, request: ExecutionRequest, workspace: Workspace, task: DshTask, *, lease_fd: int) -> ExecutionResult:
        if request.status != "running":
            raise ValueError("DSH requires a running, control-plane-approved Execution Request")
        started_at = datetime.now(timezone.utc).isoformat()
        runtime_home = private_directory(self.runtime_directory / "dsh" / workspace.workspace_id)
        home = private_directory(workspace.root / "cache" / "home")
        temporary_directory = private_directory(workspace.root / "cache" / "tmp")
        runner_path = self._write_sandbox_runner(runtime_home, workspace.root)
        patch_path = runtime_home / "provider.patch.yml"
        provider_patch = self.config.provider_patch(
            web_enabled=self.web_search_enabled() if self.web_search_enabled else None
        )
        provider_patch.extend([
            {"id": "sandbox", "config": {
                "runnerCommand": [str(runner_path)],
                "runnerFailureSignatures": ["company-agent-bwrap:"],
            }},
            *[{"id": plugin, "disabled": True} for plugin in (
                "tool-fs", "tool-fs-search", "tool-bash-persistent", "tool-pwsh",
                "tool-pwsh-persistent", "tool-terminal", "tool-str-replace-editor",
                "tool-working-directory",
            )],
        ])
        write_private_json(patch_path, provider_patch)
        environment = self.config.environment(home=home, dsh_home=runtime_home, temporary_directory=temporary_directory)
        command = self.config.resolve_command()
        version = run_process((*command, "--version"), cwd=workspace.root, env=environment, input_data=b"",
                              timeout_seconds=15, max_output_bytes=65536, lease_fd=lease_fd)
        if (version.error_category or version.returncode != 0
                or version.stdout.decode("utf-8", errors="replace").strip() != DSH_VERSION):
            return ExecutionResult(request.execution_id, "failed", error=f"DSH {DSH_VERSION} is required",
                                   metadata={"error_category": "runtime_version", "started_at": started_at,
                                             "finished_at": datetime.now(timezone.utc).isoformat()})
        self.manager.record_dependency(workspace.workspace_id, "dsh", DSH_VERSION, "official-headless")
        execution_contract = (
            "Execute the task using tools in the current workspace. Inputs are in work/, reusable scripts in tools/. "
            "Write each expected artifact under artifacts/, using exactly the relative names listed. "
            "Keep host files, runtime configuration, logs/ and state/ unchanged. "
            "Do not publish messages, access production systems or widen permissions. "
            "Report what actually ran and any failures."
        )
        if task.require_user_reply:
            begin, end = final_reply_markers(request.execution_id)
            execution_contract += (
                " The final expected artifact must contain exactly one final answer block bounded by these exact "
                f"markers: {begin} and {end}. Only the text inside that block is eligible for delivery to the user."
            )
        payload = {
            "execution_id": request.execution_id, "workspace_id": workspace.workspace_id,
            "plan_step_id": request.plan_step_id, "prompt": task.prompt,
            "expected_artifacts": list(task.expected_artifacts),
            "execution_contract": execution_contract,
        }
        process = run_process(
            (*command, "--profile", "headless", "--patch", str(patch_path), "--json",
             "--permission-mode", "workspace-write", "--approval-policy", "never", "-"),
            cwd=workspace.root, env=environment, input_data=json.dumps(payload).encode("utf-8"),
            timeout_seconds=task.timeout_seconds, max_output_bytes=task.max_output_bytes, lease_fd=lease_fd,
        )
        events, session_id, tools, output, protocol_error = self._read_protocol(process.stdout, workspace.root)
        category = process.error_category or protocol_error
        if category is None and process.returncode != 0:
            category = "process_exit"
        manifest: list[dict[str, Any]] = []
        if category is None:
            try:
                manifest = self._artifact_manifest(workspace, task)
            except (OSError, ValueError) as error:
                category = "artifact_validation"
                output = output + "\n" + str(error)
        if category is None and task.require_user_reply:
            try:
                reply_found = any(
                    extract_final_reply(
                        self._artifact_path(workspace, entry["path"].removeprefix("artifacts/")).read_text(
                            encoding="utf-8"), request.execution_id,
                    ) is not None
                    for entry in manifest
                    if isinstance(entry.get("path"), str) and entry["path"].startswith("artifacts/")
                )
            except (OSError, UnicodeError, ValueError):
                reply_found = False
            if not reply_found:
                category = "reply_contract"
                output += "\nDSH final artifact is missing a valid user-reply block"
        trace_name = f"dsh/{request.execution_id}.events.jsonl"
        self.manager.append_log(workspace.workspace_id, trace_name,
                                self.config.redact("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)))
        metadata = {
            "workspace_id": workspace.workspace_id, "cwd": ".", "session_id": session_id,
            "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
            "returncode": process.returncode, "error_category": category, "tools_used": sorted(tools),
            "stderr_bytes": len(process.stderr),
            "runtime_versions": {"dsh": DSH_VERSION, "python": platform.python_version(), "platform": platform.system()},
            "shell_sandbox": "bubblewrap-user-namespace",
            "artifact_manifest": manifest, "policy_version": POLICY_VERSION,
            "permission_mode": "workspace-write", "approval_policy": "never",
            "network_policy": "model-provider-only; shell-network-namespace-isolated",
            "limits": {"timeout_seconds": task.timeout_seconds, "max_output_bytes": task.max_output_bytes,
                       "max_artifact_bytes": task.max_artifact_bytes},
        }
        status = "succeeded" if category is None else ("timed_out" if category == "timeout" else "failed")
        return ExecutionResult(
            request.execution_id, status, self.config.redact(output),
            [f"logs/{trace_name}"],
            [entry["path"] for entry in manifest], None if category is None else f"DSH execution failed: {category}", metadata,
        )

    def _write_sandbox_runner(self, runtime_home: Path, workspace_root: Path) -> Path:
        executable = self._resolve_bubblewrap()
        host_home = Path.home().resolve()
        workspace_root = workspace_root.resolve()
        private_data_root = self.private_data_directory.resolve()
        mask_candidates = [private_data_root, host_home]
        masked_roots: list[Path] = []
        for root in sorted(set(mask_candidates), key=lambda path: len(path.parts)):
            if not any(root.is_relative_to(parent) for parent in masked_roots):
                masked_roots.append(root)
        insert = ["--tmpfs", "/mnt", "--tmpfs", "/run", "--unshare-net"]
        for root in masked_roots:
            insert.extend(["--tmpfs", str(root)])
        masked_workspace_root = next(
            (root for root in masked_roots if workspace_root.is_relative_to(root)), None
        )
        if masked_workspace_root is not None:
            relative_workspace = workspace_root.relative_to(masked_workspace_root)
            destination = masked_workspace_root
            for part in relative_workspace.parts:
                destination /= part
                insert.extend(["--dir", str(destination)])
            insert.extend(["--bind", str(workspace_root), str(workspace_root)])
        config_path = self.config.source_path
        if config_path is not None:
            config_path = config_path.expanduser().resolve()
            hidden_trees = (*masked_roots, Path("/mnt"), Path("/run"), Path("/tmp"))
            if not any(config_path.is_relative_to(root) for root in hidden_trees):
                insert.extend(["--ro-bind", "/dev/null", str(config_path)])
        runner = runtime_home / "workspace-sandbox"
        script = "\n".join([
            "#!/bin/bash",
            "set -euo pipefail",
            f"bwrap={shlex.quote(str(executable))}",
            "if [[ ! -x $bwrap ]]; then echo 'company-agent-bwrap: executable unavailable' >&2; exit 127; fi",
            "args=()",
            "inserted=0",
            "for arg in \"$@\"; do",
            "  if [[ $inserted == 0 && $arg == -- ]]; then",
            f"    args+=({ ' '.join(shlex.quote(value) for value in insert) })",
            "    inserted=1",
            "  fi",
            "  args+=(\"$arg\")",
            "done",
            "if [[ $inserted == 0 ]]; then echo 'company-agent-bwrap: invalid sandbox profile' >&2; exit 127; fi",
            "exec \"$bwrap\" \"${args[@]}\"",
            "",
        ])
        runner.write_text(script, encoding="utf-8")
        if os.name != "nt":
            runner.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return runner

    def _resolve_bubblewrap(self) -> Path:
        executable = self.bubblewrap_executable
        if executable is None:
            discovered = shutil.which("bwrap")
            executable = Path(discovered) if discovered else Path.home() / ".local/opt/bubblewrap/usr/bin/bwrap"
        executable = executable.expanduser().resolve()
        if not executable.is_file() or (os.name != "nt" and not os.access(executable, os.X_OK)):
            raise ValueError("Bubblewrap is required for DSH Shell isolation; see docs/04-step3-dsh.md")
        return executable

    @staticmethod
    def _read_protocol(data: bytes, cwd: Path) -> tuple[list[dict[str, Any]], str | None, set[str], str, str | None]:
        events: list[dict[str, Any]] = []
        session_id = None
        tools: set[str] = set()
        calls: set[str] = set()
        results: set[str] = set()
        final = None
        try:
            for line in data.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise ValueError("invalid event")
                kind = event["type"]
                if kind == "thinking":
                    continue
                if final is not None:
                    raise ValueError("events after final")
                if kind == "session":
                    if session_id or not isinstance(event.get("sessionId"), str) or not event["sessionId"]:
                        raise ValueError("invalid session")
                    if not isinstance(event.get("cwd"), str) or Path(event["cwd"]).resolve() != cwd.resolve():
                        raise ValueError("wrong execution cwd")
                    session_id = event["sessionId"]
                elif session_id is None:
                    raise ValueError("missing session")
                elif kind == "tool_call":
                    call = event.get("callId")
                    if not isinstance(call, str) or not call or call in calls or not isinstance(event.get("tool"), str):
                        raise ValueError("invalid tool call")
                    calls.add(call)
                    tools.add(event["tool"])
                    tool_input = event.get("input")
                    if isinstance(tool_input, dict) and tool_input.get("run_in_background") is True:
                        raise ValueError("background tools are not allowed in one-shot execution")
                elif kind == "tool_result":
                    call = event.get("callId")
                    if call not in calls or call in results or event.get("status") not in {"completed", "error"}:
                        raise ValueError("invalid tool result")
                    results.add(call)
                elif kind == "final":
                    if event.get("sessionId") != session_id or not isinstance(event.get("text"), str):
                        raise ValueError("invalid final")
                    final = event
                elif kind not in {"status", "text"}:
                    raise ValueError("unsupported event")
                if kind != "text":
                    events.append(event)
            if final is None or calls != results:
                raise ValueError("incomplete execution")
            reason = final.get("reason")
            if not isinstance(reason, dict) or reason.get("kind") != "completed":
                return events, session_id, tools, final["text"], "dsh_incomplete"
            return events, session_id, tools, final["text"], None
        except (ValueError, TypeError, KeyError, UnicodeError):
            return events, session_id, tools, "", "protocol_error"

    @staticmethod
    def _artifact_path(workspace: Workspace, name: str) -> Path:
        path = workspace.root / "artifacts"
        for component in ("", *PurePosixPath(name).parts):
            if component:
                path = path / component
            if path.is_symlink():
                raise ValueError(f"artifact path cannot contain symbolic links: {name}")
        if not path.resolve().is_relative_to(workspace.root / "artifacts"):
            raise ValueError("artifact path escapes artifacts/")
        return path

    def _artifact_manifest(self, workspace: Workspace, task: DshTask) -> list[dict[str, Any]]:
        manifest = []
        total_bytes = 0
        secret = self.config.api_key.encode("utf-8")
        for name in task.expected_artifacts:
            path = self._artifact_path(workspace, name)
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                total_bytes += info.st_size
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or total_bytes > task.max_artifact_bytes:
                    raise ValueError(f"artifact is not a bounded regular file: {name}")
                digest = hashlib.sha256()
                tail = b""
                while chunk := stream.read(65536):
                    if secret in tail + chunk:
                        raise ValueError(f"artifact contains a runtime credential: {name}")
                    tail = (tail + chunk)[-len(secret):]
                    digest.update(chunk)
                manifest.append({"path": f"artifacts/{name}", "bytes": info.st_size, "sha256": digest.hexdigest()})
        return manifest
