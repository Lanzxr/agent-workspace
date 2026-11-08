from __future__ import annotations

import math
import os
import platform
import shutil
import signal
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .workspace import WorkspaceManager, WorkspaceNotFound, WorkspaceStateError


@dataclass(frozen=True)
class WorkspaceLimits:
    timeout_seconds: float = 30.0
    max_output_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        if self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")


@dataclass(frozen=True)
class WorkspaceExecution:
    execution_id: str
    workspace_id: str
    command: tuple[str, ...]
    cwd: str
    status: str
    started_at: str
    finished_at: str | None
    returncode: int | None
    stdout_path: str
    stderr_path: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["command"] = list(self.command)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "WorkspaceExecution":
        data = dict(value)
        data["command"] = tuple(data["command"])
        return cls(**data)


class WorkspaceRuntime:
    """Run bounded local commands inside a Workspace for Step 2 verification."""

    def __init__(self, manager: WorkspaceManager, limits: WorkspaceLimits | None = None) -> None:
        self.manager = manager
        self.limits = limits or WorkspaceLimits()

    def preflight(self, workspace_id: str, cwd: str = "work") -> dict[str, object]:
        workspace = self.manager.get(workspace_id)
        if workspace.status != "ready":
            raise WorkspaceStateError(f"workspace is not ready: {workspace.status}")
        cwd_path = self._resolve_cwd(workspace_id, cwd)
        return {
            "workspace_id": workspace_id,
            "status": workspace.status,
            "cwd": str(cwd_path),
            "writable": os.access(cwd_path, os.W_OK),
            "platform": platform.system().lower(),
            "python": platform.python_version(),
            "limits": asdict(self.limits),
        }

    def run_command(
        self,
        workspace_id: str,
        command: Sequence[str],
        *,
        cwd: str = "work",
        env: Mapping[str, str] | None = None,
        execution_id: str | None = None,
    ) -> WorkspaceExecution:
        normalized_command = self._normalize_command(command)
        execution_id = execution_id or f"execution-{uuid.uuid4().hex}"
        self._validate_execution_id(execution_id)
        try:
            self.manager.get_execution_record(workspace_id, execution_id)
        except WorkspaceNotFound:
            pass
        else:
            raise WorkspaceStateError(f"execution already exists: {execution_id}")
        workspace = self.manager.get(workspace_id)
        cwd_path = self._resolve_cwd(workspace_id, cwd)
        workspace = self.manager.mark_running(workspace_id)
        started_at = self._timestamp()
        record = WorkspaceExecution(
            execution_id=execution_id,
            workspace_id=workspace_id,
            command=normalized_command,
            cwd=self._relative_to_workspace(workspace.root, cwd_path),
            status="running",
            started_at=started_at,
            finished_at=None,
            returncode=None,
            stdout_path=f"logs/executions/{execution_id}.stdout.log",
            stderr_path=f"logs/executions/{execution_id}.stderr.log",
            stdout_bytes=0,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
        )
        self.manager.save_execution_record(workspace_id, record.to_dict())

        process: subprocess.Popen[bytes] | None = None
        stdout = b""
        stderr = b""
        status = "failed"
        returncode: int | None = None
        error: str | None = None
        timed_out = False
        try:
            process = subprocess.Popen(
                list(normalized_command),
                cwd=cwd_path,
                env=self._environment(workspace_id, env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=os.name != "nt",
            )
            try:
                stdout, stderr = process.communicate(timeout=self.limits.timeout_seconds)
                returncode = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
                stdout, stderr = process.communicate()
                returncode = process.returncode
            status = "timed_out" if timed_out else ("succeeded" if returncode == 0 else "failed")
            if timed_out:
                error = f"execution exceeded {self.limits.timeout_seconds} seconds"
        except OSError as exception:
            error = f"process start failed: {exception}"
        finally:
            stdout_to_write, stdout_truncated = self._bounded_output(stdout)
            stderr_to_write, stderr_truncated = self._bounded_output(stderr)
            self.manager.append_log(
                workspace_id,
                str(Path(record.stdout_path).relative_to("logs")),
                stdout_to_write.decode("utf-8", errors="replace"),
            )
            self.manager.append_log(
                workspace_id,
                str(Path(record.stderr_path).relative_to("logs")),
                stderr_to_write.decode("utf-8", errors="replace"),
            )
            finished_at = self._timestamp()
            record = WorkspaceExecution(
                execution_id=record.execution_id,
                workspace_id=record.workspace_id,
                command=record.command,
                cwd=record.cwd,
                status=status,
                started_at=record.started_at,
                finished_at=finished_at,
                returncode=returncode,
                stdout_path=record.stdout_path,
                stderr_path=record.stderr_path,
                stdout_bytes=len(stdout),
                stderr_bytes=len(stderr),
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                error=error,
            )
            self.manager.save_execution_record(workspace_id, record.to_dict())
            self.manager.mark_ready(workspace_id)
        return record

    def run_python(
        self,
        workspace_id: str,
        script: str,
        *,
        cwd: str = "work",
        env: Mapping[str, str] | None = None,
        execution_id: str | None = None,
    ) -> WorkspaceExecution:
        return self.run_command(
            workspace_id,
            (sys.executable, "-c", script),
            cwd=cwd,
            env=env,
            execution_id=execution_id,
        )

    def run_shell(
        self,
        workspace_id: str,
        script: str,
        *,
        cwd: str = "work",
        env: Mapping[str, str] | None = None,
        execution_id: str | None = None,
    ) -> WorkspaceExecution:
        if os.name == "nt":
            command = ("cmd.exe", "/d", "/s", "/c", script)
        else:
            shell = shutil.which("bash") or shutil.which("sh")
            if shell is None:
                raise WorkspaceStateError("no POSIX shell is available")
            command = (shell, "-lc" if Path(shell).name == "bash" else "-c", script)
        return self.run_command(workspace_id, command, cwd=cwd, env=env, execution_id=execution_id)

    def get_execution(self, workspace_id: str, execution_id: str) -> WorkspaceExecution:
        value = self.manager.get_execution_record(workspace_id, execution_id)
        return WorkspaceExecution.from_dict(value)

    def read_logs(self, workspace_id: str, execution_id: str) -> dict[str, str]:
        execution = self.get_execution(workspace_id, execution_id)
        stdout = self.manager.validate_path(workspace_id, execution.stdout_path).read_text(encoding="utf-8")
        stderr = self.manager.validate_path(workspace_id, execution.stderr_path).read_text(encoding="utf-8")
        return {"stdout": stdout, "stderr": stderr}

    def _resolve_cwd(self, workspace_id: str, cwd: str) -> Path:
        relative = Path(cwd)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("cwd must be a relative path inside the workspace")
        path = self.manager.validate_path(workspace_id, relative)
        if not path.is_dir():
            raise WorkspaceStateError(f"workspace cwd is not a directory: {cwd}")
        return path

    @staticmethod
    def _relative_to_workspace(root: Path, path: Path) -> str:
        return str(path.relative_to(root))

    @staticmethod
    def _normalize_command(command: Sequence[str]) -> tuple[str, ...]:
        values = tuple(command)
        if not values or any(not isinstance(value, str) or not value for value in values):
            raise ValueError("command must contain at least one non-empty string")
        return values

    @staticmethod
    def _validate_execution_id(execution_id: str) -> None:
        if not execution_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in execution_id):
            raise ValueError("execution_id contains unsupported characters")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _environment(workspace_id: str, extra: Mapping[str, str] | None) -> dict[str, str]:
        safe_names = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SystemRoot", "WINDIR", "PATHEXT", "USERPROFILE"}
        environment = {name: os.environ[name] for name in safe_names if name in os.environ}
        environment["COMPANY_AGENT_WORKSPACE_ID"] = workspace_id
        if extra:
            for name, value in extra.items():
                if not isinstance(name, str) or not name.strip() or not isinstance(value, str):
                    raise ValueError("environment names and values must be strings")
                environment[name] = value
        return environment

    def _bounded_output(self, value: bytes) -> tuple[bytes, bool]:
        if len(value) <= self.limits.max_output_bytes:
            return value, False
        return value[: self.limits.max_output_bytes], True

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if os.name == "nt":
            process.kill()
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
