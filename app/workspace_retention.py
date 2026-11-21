from __future__ import annotations

import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .execution_state import ExecutionBusy
from .workspace import Workspace, WorkspaceManager

DEFAULT_WORKSPACE_RETENTION_DAYS = 15
MIN_WORKSPACE_RETENTION_DAYS = 1
MAX_WORKSPACE_RETENTION_DAYS = 365
WORKSPACE_RETENTION_SETTING_KEY = "workspace_file_retention_days"

_MANAGED_SUBDIRS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("work", "inputs"), "inputs"),
    (("work", "outputs"), "outputs"),
    (("work", "out"), "legacy_out"),
    (("artifacts",), "artifacts"),
)


def normalize_retention_days(value: Any, *, default: int = DEFAULT_WORKSPACE_RETENTION_DAYS) -> int:
    """Validate and coerce the configured workspace retention window in days."""
    if isinstance(value, bool):
        return default
    try:
        days = int(value)
    except (TypeError, ValueError):
        return default
    if not MIN_WORKSPACE_RETENTION_DAYS <= days <= MAX_WORKSPACE_RETENTION_DAYS:
        raise ValueError(
            f"workspace file retention days must be between {MIN_WORKSPACE_RETENTION_DAYS} and {MAX_WORKSPACE_RETENTION_DAYS}"
        )
    return days


def get_retention_days(plane: Any) -> int:
    """Read the persisted retention window (in days) from control-plane settings."""
    raw = plane.get_setting(WORKSPACE_RETENTION_SETTING_KEY, DEFAULT_WORKSPACE_RETENTION_DAYS)
    try:
        return normalize_retention_days(raw, default=DEFAULT_WORKSPACE_RETENTION_DAYS)
    except ValueError:
        return DEFAULT_WORKSPACE_RETENTION_DAYS


def set_retention_days(plane: Any, days: Any) -> int:
    """Validate and persist the workspace file retention window in days."""
    if isinstance(days, bool):
        raise ValueError("retention_days must be an integer")
    try:
        parsed = int(days)
    except (TypeError, ValueError) as error:
        raise ValueError("retention_days must be an integer") from error
    validated = normalize_retention_days(parsed)
    plane.set_setting(WORKSPACE_RETENTION_SETTING_KEY, validated)
    return validated


def _has_symlink_component(path: Path, stop_at: Path) -> bool:
    current = path
    while current != stop_at and current.is_relative_to(stop_at):
        if current.is_symlink():
            return True
        current = current.parent
    return stop_at.is_symlink()


def resolve_workspace_file(manager: WorkspaceManager, workspace_id: str, relative_path: str,
                           *, allowed_roots: tuple[str, ...] = ("work", "artifacts")) -> Path:
    """Safely resolve a non-symlink regular file inside work/ or artifacts/ of one Workspace."""
    if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path or ":" in relative_path:
        raise ValueError("relative file path must be a relative POSIX path")
    posix = PurePosixPath(relative_path)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise ValueError("relative file path escapes workspace root")
    if posix.parts[0] not in allowed_roots:
        raise ValueError(f"file path must stay under {', '.join(allowed_roots)}")
    workspace = manager.get(workspace_id)
    root = workspace.root.resolve(strict=True)
    candidate = root.joinpath(*posix.parts)
    if _has_symlink_component(candidate, root):
        raise ValueError("workspace file path cannot traverse symbolic links")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("workspace file path escapes workspace root")
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("workspace file path is not a regular file")
    return resolved


def touch_workspace_files(manager: WorkspaceManager, workspace_id: str, relative_paths: Iterable[str]) -> int:
    """Refresh mtimes on actively referenced workspace files so TTL counts from last session use."""
    touched = 0
    for rel in relative_paths:
        if not isinstance(rel, str) or not rel:
            continue
        try:
            resolved = resolve_workspace_file(manager, workspace_id, rel)
            os.utime(resolved, None)
            touched += 1
        except (OSError, ValueError):
            continue
    return touched


def _iter_workspace_ids(manager: WorkspaceManager, workspace_id: str | None) -> list[str]:
    if workspace_id is not None:
        return [workspace_id]
    base = manager.base_directory
    if not base.is_dir():
        return []
    ids: list[str] = []
    for entry in sorted(base.iterdir()):
        if entry.is_symlink() or not entry.is_dir() or entry.name.startswith("."):
            continue
        if (entry / "state" / "workspace.json").is_file():
            ids.append(entry.name)
    return ids


def _managed_root(workspace: Workspace, parts: tuple[str, ...]) -> Path | None:
    root = workspace.root.resolve()
    target = root.joinpath(*parts)
    if not target.exists():
        return None
    if _has_symlink_component(target, root) or not target.is_dir():
        return None
    return target


def list_workspace_files(
    manager: WorkspaceManager,
    *,
    workspace_id: str | None = None,
    retention_days: int = DEFAULT_WORKSPACE_RETENTION_DAYS,
    limit: int = 200,
    include_artifacts: bool = True,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """List retained working and deliverable files across one or all Workspaces, newest first."""
    days = normalize_retention_days(retention_days)
    now_ts = time.time() if now is None else float(now)
    records: list[dict[str, Any]] = []
    for ws_id in _iter_workspace_ids(manager, workspace_id):
        try:
            workspace = manager.get(ws_id)
        except Exception:
            continue
        ws_root = workspace.root.resolve()
        for parts, category in _MANAGED_SUBDIRS:
            if category == "artifacts" and not include_artifacts:
                continue
            subdir = _managed_root(workspace, parts)
            if subdir is None:
                continue
            for dirpath, dirnames, filenames in os.walk(subdir, followlinks=False):
                current_dir = Path(dirpath)
                dirnames[:] = [
                    d for d in dirnames
                    if not (current_dir / d).is_symlink() and d != "__pycache__"
                ]
                for fname in filenames:
                    if fname.endswith(".partial") or fname.endswith(".tmp") or fname.startswith("~$"):
                        continue
                    fpath = current_dir / fname
                    if fpath.is_symlink():
                        continue
                    try:
                        info = fpath.stat()
                    except OSError:
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    rel = fpath.relative_to(ws_root).as_posix()
                    age_seconds = max(0.0, now_ts - info.st_mtime)
                    age_days = round(age_seconds / 86400.0, 2)
                    remaining_days = round(max(0.0, days - (age_seconds / 86400.0)), 2)
                    mod_dt = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
                    exp_dt = mod_dt + timedelta(days=days)
                    records.append({
                        "workspace_id": ws_id,
                        "tier": workspace.tier,
                        "category": category,
                        "relative_path": rel,
                        "filename": fname,
                        "size_bytes": info.st_size,
                        "modified_epoch": info.st_mtime,
                        "modified_at": mod_dt.isoformat(),
                        "expires_at": exp_dt.isoformat(),
                        "age_days": age_days,
                        "remaining_days": remaining_days,
                    })
    records.sort(key=lambda item: (-float(item["modified_epoch"]), str(item["relative_path"])))
    for item in records:
        item.pop("modified_epoch", None)
    return records[: max(1, min(int(limit), 1000))]


def summarize_recent_workspace_files(
    manager: WorkspaceManager,
    workspace_id: str,
    *,
    retention_days: int = DEFAULT_WORKSPACE_RETENTION_DAYS,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Return compact metadata of non-expired input/output files for follow-up session reuse."""
    items = list_workspace_files(
        manager,
        workspace_id=workspace_id,
        retention_days=retention_days,
        limit=60,
        include_artifacts=False,
    )
    compact: list[dict[str, Any]] = []
    for item in items:
        if item["category"] not in {"inputs", "outputs"}:
            continue
        compact.append({
            "category": item["category"],
            "path": item["relative_path"],
            "filename": item["filename"],
            "size_bytes": item["size_bytes"],
            "modified_at": item["modified_at"],
        })
        if len(compact) >= limit:
            break
    return compact


def prune_expired_workspace_files(
    manager: WorkspaceManager,
    *,
    retention_days: int = DEFAULT_WORKSPACE_RETENTION_DAYS,
    now: float | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Remove workspace working/artifact files older than retention_days without disturbing live tasks."""
    days = normalize_retention_days(retention_days)
    now_ts = time.time() if now is None else float(now)
    cutoff = now_ts - days * 86400.0
    checked_workspaces = 0
    skipped_active = 0
    deleted_files = 0
    freed_bytes = 0

    for ws_id in _iter_workspace_ids(manager, workspace_id):
        try:
            with manager.execution_lease(ws_id):
                workspace = manager.get(ws_id)
                if workspace.status == "running":
                    skipped_active += 1
                    continue
                checked_workspaces += 1
                for parts, _category in _MANAGED_SUBDIRS:
                    subdir = _managed_root(workspace, parts)
                    if subdir is None:
                        continue
                    for dirpath, dirnames, filenames in os.walk(subdir, topdown=False, followlinks=False):
                        current_dir = Path(dirpath)
                        if current_dir.is_symlink():
                            continue
                        for fname in filenames:
                            fpath = current_dir / fname
                            if fpath.is_symlink():
                                continue
                            try:
                                info = fpath.stat()
                                if not stat.S_ISREG(info.st_mode):
                                    continue
                                is_stale_partial = fname.endswith(".partial") and info.st_mtime < (now_ts - 3600.0)
                                if is_stale_partial or info.st_mtime < cutoff:
                                    size = info.st_size
                                    fpath.unlink(missing_ok=True)
                                    deleted_files += 1
                                    freed_bytes += size
                            except OSError:
                                continue
                        if current_dir != subdir:
                            try:
                                if not any(current_dir.iterdir()):
                                    current_dir.rmdir()
                            except OSError:
                                pass
        except ExecutionBusy:
            skipped_active += 1
        except Exception:
            continue

    return {
        "retention_days": days,
        "checked_workspaces": checked_workspaces,
        "skipped_active": skipped_active,
        "deleted_files": deleted_files,
        "freed_bytes": freed_bytes,
    }
