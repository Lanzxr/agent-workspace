from __future__ import annotations

import json
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

from .workspace import WorkspaceManager, WorkspaceNotFound, WorkspaceStateError, validate_execution_id


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
        with self.manager.execution_lease(workspace_id):
            return self._run_command(workspace_id, command, cwd=cwd, env=env, execution_id=execution_id)

    def _run_command(
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
        validate_execution_id(execution_id)
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
        record = self.manager.get_execution_record(workspace_id, execution_id)
        if record.get("runtime") == "dsh":
            return self._read_dsh_logs(workspace_id, execution_id, record)
        execution = WorkspaceExecution.from_dict(record)
        stdout = self.manager.validate_path(workspace_id, execution.stdout_path).read_text(encoding="utf-8")
        stderr = self.manager.validate_path(workspace_id, execution.stderr_path).read_text(encoding="utf-8")
        return {"stdout": stdout, "stderr": stderr}

    def _read_dsh_logs(self, workspace_id: str, execution_id: str, record: dict[str, object]) -> dict[str, str]:
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        raw_paths = result.get("log_paths") if isinstance(result.get("log_paths"), list) else []
        log_paths = [str(p) for p in raw_paths if isinstance(p, str) and p.strip()] or [
            f"logs/dsh/{execution_id}.events.jsonl"
        ]
        summary_lines = [
            f"execution_id: {execution_id} | runtime: dsh | status: {record.get('status') or result.get('status') or 'unknown'}",
        ]
        if metadata.get("started_at") or metadata.get("finished_at"):
            summary_lines.append(f"time: {metadata.get('started_at') or '-'} -> {metadata.get('finished_at') or '-'}")
        if metadata.get("tools_used"):
            summary_lines.append(f"tools_used: {', '.join(str(t) for t in metadata['tools_used'])}")
        if result.get("artifact_paths"):
            summary_lines.append(f"artifacts: {', '.join(str(a) for a in result['artifact_paths'])}")

        trace_lines: list[str] = []
        for rel_path in log_paths:
            try:
                log_file = self.manager.validate_path(workspace_id, rel_path)
            except ValueError:
                continue
            if not log_file.is_file() or log_file.is_symlink():
                continue
            text = log_file.read_text(encoding="utf-8", errors="replace")
            if len(text) > 150_000:
                text = text[-150_000:]
            for raw in text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                trace_lines.append(_format_dsh_event_line(line))

        output = str(result.get("output") or "").strip()
        stdout_parts = ["\n".join(summary_lines)]
        if trace_lines:
            stdout_parts.append("--- DSH 执行轨迹 ---\n" + "\n".join(trace_lines))
        if output:
            stdout_parts.append("--- 输出摘要 ---\n" + output[:12000])

        stderr_parts: list[str] = []
        if metadata.get("error_category"):
            stderr_parts.append(f"error_category: {metadata['error_category']}")
        if result.get("error"):
            stderr_parts.append(str(result["error"]))
        return {"stdout": "\n\n".join(stdout_parts), "stderr": "\n".join(stderr_parts)}

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


def _format_dsh_event_line(raw_line: str) -> str:
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        return raw_line[:500]
    if not isinstance(event, dict):
        return raw_line[:500]
    kind = str(event.get("type") or "event")
    if kind == "tool_call":
        tool = event.get("tool") or event.get("name") or "tool"
        inp = json.dumps(event.get("input", {}), ensure_ascii=False)
        if len(inp) > 300:
            inp = inp[:300] + "..."
        return f"[tool_call] {tool}: {inp}"
    if kind == "tool_result":
        status = event.get("status") or ""
        res = event.get("result")
        text = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
        if len(text) > 500:
            text = text[:500] + "..."
        return f"[tool_result] ({status}) {text}"
    if kind == "status":
        parts = [f"{k}={event[k]}" for k in ("phase", "turn", "step") if k in event]
        return f"[status] {' '.join(parts)}"
    if kind in {"thinking", "text", "final"}:
        body = str(event.get("text") or event.get("content") or event.get("message") or "")
        if len(body) > 1000:
            body = body[:1000] + "..."
        return f"[{kind}] {body}" if body else f"[{kind}]"
    compact = json.dumps(event, ensure_ascii=False)
    return compact[:400] + ("..." if len(compact) > 400 else "")
