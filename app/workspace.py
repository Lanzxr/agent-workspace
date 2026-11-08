from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


WORKSPACE_DIRECTORIES = ("work", "tools", "artifacts", "logs", "state", "cache")
WORKSPACE_MANIFEST = "workspace.json"
WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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

    def restore(self, workspace_id: str) -> Workspace:
        workspace = self._load(workspace_id)
        self._ensure_layout(workspace.root)
        if workspace.status == "running":
            workspace = self._replace_status(workspace, "ready")
            self._write_manifest(workspace)
        return workspace

    def mark_running(self, workspace_id: str) -> Workspace:
        workspace = self._load(workspace_id)
        if workspace.status != "ready":
            raise WorkspaceStateError(f"workspace cannot run from {workspace.status}")
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
        return self._write_text(workspace_id, "work", relative_path, content)

    def save_artifact(self, workspace_id: str, relative_path: str, content: str) -> Path:
        return self._write_text(workspace_id, "artifacts", relative_path, content)

    def append_log(self, workspace_id: str, relative_path: str, content: str) -> Path:
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
        }

    def _load(self, workspace_id: str) -> Workspace:
        self._validate_workspace_id(workspace_id)
        root = self._workspace_root(workspace_id)
        manifest_path = root / "state" / WORKSPACE_MANIFEST
        if not manifest_path.is_file():
            raise WorkspaceNotFound(f"workspace not found: {workspace_id}")
        return self._read_manifest(manifest_path, workspace_id, root)

    def _workspace_root(self, workspace_id: str) -> Path:
        root = (self.base_directory / workspace_id).resolve()
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
        root.mkdir(parents=True, exist_ok=True)
        self._secure_directory(root)
        for name in WORKSPACE_DIRECTORIES:
            directory = root / name
            directory.mkdir(parents=True, exist_ok=True)
            self._secure_directory(directory)

    def _read_manifest(self, path: Path, workspace_id: str, root: Path) -> Workspace:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("workspace_id") != workspace_id:
                raise WorkspaceError("workspace manifest id does not match its directory")
            status = data["status"]
            created_at = data["created_at"]
            updated_at = data["updated_at"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise WorkspaceError(f"invalid workspace manifest: {path}") from error
        return Workspace(workspace_id, root, status, created_at, updated_at)

    def _write_manifest(self, workspace: Workspace) -> None:
        path = workspace.directory("state") / WORKSPACE_MANIFEST
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
