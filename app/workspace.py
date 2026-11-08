from __future__ import annotations

import json
import os
import platform
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE_DIRECTORIES = ("work", "tools", "artifacts", "logs", "state", "cache")
WORKSPACE_MANIFEST = "workspace.json"
WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
WORKSPACE_STATUSES = {"provisioned", "ready", "running", "suspended", "recovering", "failed", "deleted"}


def validate_execution_id(execution_id: str) -> None:
    if not isinstance(execution_id, str) or not EXECUTION_ID_PATTERN.fullmatch(execution_id):
        raise ValueError("execution_id contains unsupported characters")


class WorkspaceError(Exception):
    """Base error for Workspace lifecycle and boundary failures."""


class WorkspaceNotFound(WorkspaceError):
    """Raised when a Workspace has no readable manifest."""


class WorkspacePathError(WorkspaceError):
    """Raised when a path is outside the Workspace boundary."""


class WorkspaceStateError(WorkspaceError):
    """Raised when a Workspace lifecycle transition is invalid."""


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    root: Path
    status: str
    created_at: str
    updated_at: str

    def directory(self, name: str) -> Path:
        if name not in WORKSPACE_DIRECTORIES:
            raise WorkspacePathError(f"unknown workspace directory: {name}")
        return self.root / name


class WorkspaceManager:
    """Own the lifecycle and filesystem boundary of Agent Workspaces."""

    def __init__(self, base_directory: Path) -> None:
        self.base_directory = Path(base_directory).expanduser().resolve()
        self.base_directory.mkdir(parents=True, exist_ok=True)
        self._secure_directory(self.base_directory)

    def provision(self, workspace_id: str) -> Workspace:
        self._validate_workspace_id(workspace_id)
        root = self._workspace_root(workspace_id)
        manifest_path = root / "state" / WORKSPACE_MANIFEST
        if manifest_path.is_symlink():
            raise WorkspacePathError(f"workspace manifest cannot be a symbolic link: {manifest_path}")
        if manifest_path.is_file():
            workspace = self._read_manifest(manifest_path, workspace_id, root)
            if workspace.status == "deleted":
                raise WorkspaceStateError(f"workspace is deleted: {workspace_id}")
            self._ensure_layout(root)
            return workspace

        if root.exists() and not root.is_dir():
            raise WorkspaceError(f"workspace root is not a directory: {root}")

        self._ensure_layout(root)
        timestamp = self._timestamp()
        workspace = Workspace(workspace_id, root, "ready", timestamp, timestamp)
        self._write_manifest(workspace)
        return workspace

    def get(self, workspace_id: str) -> Workspace:
        return self._load(workspace_id)

    def restore(self, workspace_id: str) -> Workspace:
        workspace = self._load(workspace_id)
        if workspace.status == "deleted":
            raise WorkspaceStateError(f"deleted workspace cannot be restored: {workspace_id}")
        self._ensure_layout(workspace.root)
        self._recover_execution_records(workspace_id)
        if workspace.status in {"running", "recovering"}:
            recovering = self._replace_status(workspace, "recovering")
            self._write_manifest(recovering)
            workspace = self._replace_status(recovering, "ready")
            self._write_manifest(workspace)
        return workspace

    def mark_running(self, workspace_id: str) -> Workspace:
        workspace = self._load(workspace_id)
        if workspace.status != "ready":
            raise WorkspaceStateError(f"workspace cannot run from {workspace.status}")
        if any(execution.get("status") == "running" for execution in self.list_executions(workspace_id)):
            raise WorkspaceStateError("workspace already has a running execution")
        workspace = self._replace_status(workspace, "running")
        self._write_manifest(workspace)
        return workspace

    def mark_suspended(self, workspace_id: str) -> Workspace:
        workspace = self._load(workspace_id)
        if workspace.status not in {"ready", "running"}:
            raise WorkspaceStateError(f"workspace cannot suspend from {workspace.status}")
        workspace = self._replace_status(workspace, "suspended")
        self._write_manifest(workspace)
        return workspace

    def mark_failed(self, workspace_id: str) -> Workspace:
        workspace = self._load(workspace_id)
        if workspace.status == "deleted":
            raise WorkspaceStateError("deleted workspace cannot fail")
        workspace = self._replace_status(workspace, "failed")
        self._write_manifest(workspace)
        return workspace

    def mark_ready(self, workspace_id: str) -> Workspace:
        workspace = self._load(workspace_id)
        if workspace.status not in {"running", "recovering"}:
            raise WorkspaceStateError(f"workspace cannot return to ready from {workspace.status}")
        workspace = self._replace_status(workspace, "ready")
        self._write_manifest(workspace)
        return workspace

    def mark_deleted(self, workspace_id: str) -> Workspace:
        workspace = self._load(workspace_id)
        if workspace.status == "running":
            raise WorkspaceStateError("running workspace cannot be deleted")
        workspace = self._replace_status(workspace, "deleted")
        self._write_manifest(workspace)
        return workspace

    def validate_path(self, workspace_id: str, path: Path) -> Path:
        workspace = self._load(workspace_id)
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = workspace.root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(workspace.root.resolve())
        except ValueError as error:
            raise WorkspacePathError(f"path is outside workspace: {path}") from error
        return resolved

    def save_input(self, workspace_id: str, relative_path: str, content: str) -> Path:
        self._ensure_writable(workspace_id)
        return self._write_text(workspace_id, "work", relative_path, content)

    def save_artifact(self, workspace_id: str, relative_path: str, content: str) -> Path:
        self._ensure_writable(workspace_id)
        return self._write_text(workspace_id, "artifacts", relative_path, content)

    def append_log(self, workspace_id: str, relative_path: str, content: str) -> Path:
        self._ensure_writable(workspace_id)
        path = self._safe_child(workspace_id, "logs", relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(content)
        self._secure_file(path)
        return path

    def summary(self, workspace_id: str) -> dict[str, object]:
        workspace = self._load(workspace_id)
        return {
            "workspace_id": workspace.workspace_id,
            "root": str(workspace.root),
            "status": workspace.status,
            "created_at": workspace.created_at,
            "updated_at": workspace.updated_at,
            "directories": {name: str(workspace.directory(name)) for name in WORKSPACE_DIRECTORIES},
            "capabilities": self.capabilities(workspace_id),
            "dependencies": self.list_dependencies(workspace_id),
            "executions": self.list_executions(workspace_id),
            "artifacts": self._list_files(workspace.directory("artifacts")),
            "logs": self._list_files(workspace.directory("logs")),
        }

    def capabilities(self, workspace_id: str) -> dict[str, object]:
        workspace = self._load(workspace_id)
        return {
            "platform": platform.system().lower(),
            "python": platform.python_version(),
            "workspace_status": workspace.status,
            "path_boundary": "workspace-root",
            "posix_permissions": os.name != "nt",
            "directories": list(WORKSPACE_DIRECTORIES),
        }

    def record_dependency(self, workspace_id: str, name: str, version: str, source: str = "runtime") -> dict[str, str]:
        self._ensure_writable(workspace_id)
        if not name.strip() or not version.strip() or not source.strip():
            raise ValueError("dependency name, version, and source are required")
        dependencies = self.list_dependencies(workspace_id)
        dependency = {"name": name, "version": version, "source": source}
        dependencies = [item for item in dependencies if item["name"] != name]
        dependencies.append(dependency)
        dependencies.sort(key=lambda item: item["name"])
        self._write_json(self._state_file(workspace_id, "dependencies.json"), dependencies)
        return dependency

    def list_dependencies(self, workspace_id: str) -> list[dict[str, str]]:
        path = self._state_file(workspace_id, "dependencies.json")
        if path.is_symlink():
            raise WorkspacePathError(f"workspace dependency manifest cannot be a symbolic link: {path}")
        if not path.is_file():
            return []
        value = self._read_json(path)
        if not isinstance(value, list):
            raise WorkspaceError(f"invalid dependency manifest: {path}")
        return [item for item in value if isinstance(item, dict)]

    def list_executions(self, workspace_id: str) -> list[dict[str, object]]:
        execution_directory = self._state_directory(workspace_id) / "executions"
        if not execution_directory.exists():
            return []
        if execution_directory.is_symlink() or not execution_directory.is_dir():
            raise WorkspacePathError(f"workspace execution directory is invalid: {execution_directory}")
        executions: list[dict[str, object]] = []
        for path in sorted(execution_directory.glob("*.json")):
            if path.is_symlink():
                raise WorkspacePathError(f"workspace execution record cannot be a symbolic link: {path}")
            value = self._read_json(path)
            if isinstance(value, dict):
                executions.append(value)
        return executions

    def save_execution_record(self, workspace_id: str, record: dict[str, object]) -> None:
        self._ensure_writable_or_recovering(workspace_id)
        execution_id = record.get("execution_id")
        validate_execution_id(execution_id)
        execution_directory = self._state_directory(workspace_id) / "executions"
        if execution_directory.is_symlink():
            raise WorkspacePathError(f"workspace execution directory is invalid: {execution_directory}")
        execution_directory.mkdir(parents=True, exist_ok=True)
        self._secure_directory(execution_directory)
        self._write_json(execution_directory / f"{execution_id}.json", record)

    def get_execution_record(self, workspace_id: str, execution_id: str) -> dict[str, object]:
        validate_execution_id(execution_id)
        path = self._state_directory(workspace_id) / "executions" / f"{execution_id}.json"
        if not path.is_file() or path.is_symlink():
            raise WorkspaceNotFound(f"execution not found: {execution_id}")
        value = self._read_json(path)
        if not isinstance(value, dict):
            raise WorkspaceError(f"invalid execution record: {path}")
        return value

    def _load(self, workspace_id: str) -> Workspace:
        self._validate_workspace_id(workspace_id)
        root = self._workspace_root(workspace_id)
        manifest_path = root / "state" / WORKSPACE_MANIFEST
        if manifest_path.is_symlink():
            raise WorkspacePathError(f"workspace manifest cannot be a symbolic link: {manifest_path}")
        if not manifest_path.is_file():
            raise WorkspaceNotFound(f"workspace not found: {workspace_id}")
        return self._read_manifest(manifest_path, workspace_id, root)

    def _state_directory(self, workspace_id: str) -> Path:
        workspace = self._load(workspace_id)
        state_directory = workspace.directory("state")
        if state_directory.is_symlink() or not state_directory.is_dir():
            raise WorkspacePathError(f"workspace state directory is invalid: {state_directory}")
        return state_directory

    def _state_file(self, workspace_id: str, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            raise ValueError("state file name is invalid")
        return self._state_directory(workspace_id) / name

    def _workspace_root(self, workspace_id: str) -> Path:
        raw_root = self.base_directory / workspace_id
        if raw_root.is_symlink():
            raise WorkspacePathError(f"workspace root cannot be a symbolic link: {workspace_id}")
        root = raw_root.resolve()
        try:
            root.relative_to(self.base_directory)
        except ValueError as error:
            raise WorkspacePathError(f"workspace is outside base directory: {workspace_id}") from error
        return root

    @staticmethod
    def _validate_workspace_id(workspace_id: str) -> None:
        if not isinstance(workspace_id, str) or not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
            raise ValueError("workspace_id must be 1-64 letters, numbers, dots, underscores, or hyphens")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _ensure_layout(self, root: Path) -> None:
        if root.is_symlink():
            raise WorkspacePathError(f"workspace root cannot be a symbolic link: {root}")
        root.mkdir(parents=True, exist_ok=True)
        self._secure_directory(root)
        for name in WORKSPACE_DIRECTORIES:
            directory = root / name
            if directory.is_symlink():
                raise WorkspacePathError(f"workspace directory cannot be a symbolic link: {directory}")
            if directory.exists() and not directory.is_dir():
                raise WorkspaceError(f"workspace path is not a directory: {directory}")
            directory.mkdir(parents=True, exist_ok=True)
            self._secure_directory(directory)

    def _ensure_writable(self, workspace_id: str) -> Workspace:
        workspace = self._load(workspace_id)
        if workspace.status not in {"ready", "running"}:
            raise WorkspaceStateError(f"workspace is not writable from {workspace.status}")
        return workspace

    def _read_manifest(self, path: Path, workspace_id: str, root: Path) -> Workspace:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("workspace_id") != workspace_id:
                raise WorkspaceError("workspace manifest id does not match its directory")
            status = data["status"]
            if status not in WORKSPACE_STATUSES:
                raise WorkspaceError(f"unsupported workspace status: {status}")
            created_at = data["created_at"]
            updated_at = data["updated_at"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise WorkspaceError(f"invalid workspace manifest: {path}") from error
        return Workspace(workspace_id, root, status, created_at, updated_at)

    def _write_manifest(self, workspace: Workspace) -> None:
        state_directory = workspace.directory("state")
        if state_directory.is_symlink() or not state_directory.is_dir():
            raise WorkspacePathError(f"workspace state directory is invalid: {state_directory}")
        path = state_directory / WORKSPACE_MANIFEST
        temporary_path = path.with_suffix(".tmp")
        payload = {
            "version": 1,
            "workspace_id": workspace.workspace_id,
            "status": workspace.status,
            "created_at": workspace.created_at,
            "updated_at": workspace.updated_at,
            "directories": list(WORKSPACE_DIRECTORIES),
        }
        temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._secure_file(temporary_path)
        temporary_path.replace(path)
        self._secure_file(path)

    @staticmethod
    def _read_json(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkspaceError(f"invalid workspace state file: {path}") from error

    def _write_json(self, path: Path, value: object) -> None:
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        temporary_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self._secure_file(temporary_path)
        temporary_path.replace(path)
        self._secure_file(path)

    def _list_files(self, directory: Path) -> list[dict[str, object]]:
        if directory.is_symlink() or not directory.is_dir():
            raise WorkspacePathError(f"workspace directory is invalid: {directory}")
        files: list[dict[str, object]] = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            files.append({"path": str(path.relative_to(directory)), "bytes": path.stat().st_size})
        return files

    def _recover_execution_records(self, workspace_id: str) -> list[str]:
        self._ensure_writable_or_recovering(workspace_id)
        recovered: list[str] = []
        execution_directory = self._state_directory(workspace_id) / "executions"
        if not execution_directory.exists():
            return recovered
        if execution_directory.is_symlink() or not execution_directory.is_dir():
            raise WorkspacePathError(f"workspace execution directory is invalid: {execution_directory}")
        for path in sorted(execution_directory.glob("*.json")):
            if path.is_symlink():
                raise WorkspacePathError(f"workspace execution record cannot be a symbolic link: {path}")
            value = self._read_json(path)
            if not isinstance(value, dict) or value.get("status") != "running":
                continue
            value["status"] = "interrupted"
            value["finished_at"] = self._timestamp()
            value["error"] = "execution interrupted before workspace recovery"
            self._write_json(path, value)
            recovered.append(str(value.get("execution_id", path.stem)))
        return recovered

    def _ensure_writable_or_recovering(self, workspace_id: str) -> Workspace:
        workspace = self._load(workspace_id)
        if workspace.status not in {"ready", "running", "recovering"}:
            raise WorkspaceStateError(f"workspace cannot recover from {workspace.status}")
        return workspace

    def _replace_status(self, workspace: Workspace, status: str) -> Workspace:
        return Workspace(workspace.workspace_id, workspace.root, status, workspace.created_at, self._timestamp())

    def _safe_child(self, workspace_id: str, directory: str, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise WorkspacePathError(f"relative path escapes workspace: {relative_path}")
        return self.validate_path(workspace_id, Path(directory) / relative)

    def _write_text(self, workspace_id: str, directory: str, relative_path: str, content: str) -> Path:
        path = self._safe_child(workspace_id, directory, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._secure_file(path)
        return path

    @staticmethod
    def _secure_directory(path: Path) -> None:
        if os.name != "nt":
            path.chmod(0o700)

    @staticmethod
    def _secure_file(path: Path) -> None:
        if os.name != "nt":
            path.chmod(0o600)
