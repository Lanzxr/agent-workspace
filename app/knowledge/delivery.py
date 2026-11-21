from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..models import Event
from ..workspace import Workspace, WorkspaceManager
from .service import KnowledgeService


@dataclass(frozen=True)
class StagedKnowledgeFile:
    document_id: str
    filename: str
    relative_path: str
    content_hash: str


class KnowledgeDelivery:
    """Copies only authorized, selected originals into one DSH execution's inputs directory."""

    def __init__(self, service: KnowledgeService, workspaces: WorkspaceManager) -> None:
        self.service = service
        self.workspaces = workspaces

    def stage(self, event: Event, workspace: Workspace, execution_id: str,
              document_ids: list[str], attachment_ids: list[str] | None = None) -> list[StagedKnowledgeFile]:
        documents = self.service.authorize_documents(event, document_ids)
        attachments = self.service.authorize_attachments(event, attachment_ids or [], require_original=True)
        if not documents and not attachments:
            return []
        relative_root = Path("work") / "inputs" / execution_id
        inputs_root = workspace.directory("work") / "inputs"
        if inputs_root.is_symlink():
            raise ValueError("knowledge inputs directory cannot be a symbolic link")
        inputs_root.mkdir(exist_ok=True)
        lexical_root = inputs_root / execution_id
        if lexical_root.is_symlink():
            raise ValueError("reserved knowledge input directory cannot be a symbolic link")
        root = self.workspaces.validate_path(workspace.workspace_id, lexical_root)
        if root.exists():
            if not root.is_dir():
                raise ValueError("reserved knowledge input path is not a directory")
            # Remove abandoned partial staging artifacts before validating or refreshing inputs.
            for partial in root.glob("*.partial"):
                if not partial.is_symlink() and partial.is_file():
                    partial.unlink(missing_ok=True)
        else:
            root.mkdir(parents=True, mode=0o700)
        staged: list[StagedKnowledgeFile] = []
        try:
            sources = [("document", item) for item in documents] + [("attachment", item) for item in attachments]
            seen_hashes: set[str] = set()
            for index, (source_type, document) in enumerate(sources, start=1):
                digest_value = document["content_hash"]
                if digest_value in seen_hashes:
                    continue
                seen_hashes.add(digest_value)
                filename = _safe_filename(document["filename"])
                target_name = f"{index:02d}-{filename}"
                target = self.workspaces.validate_path(workspace.workspace_id, root / target_name)
                if target.is_symlink():
                    raise ValueError("staged knowledge file cannot be a symbolic link")
                if target.is_file() and _sha256(target) == digest_value:
                    os.utime(target, None)
                    reference_id = document.get("document_id") if source_type == "document" else document["attachment_id"]
                    staged.append(StagedKnowledgeFile(reference_id, filename,
                                                      (relative_root / target_name).as_posix(), digest_value))
                    continue
                partial = target.with_name(target.name + ".partial")
                source_value = document.get("original_path")
                if not isinstance(source_value, str) or not source_value:
                    raise ValueError("selected original file is not available")
                source = Path(source_value).resolve(strict=True)
                if not source.is_relative_to(self.service.storage_directory) or source.is_symlink():
                    raise ValueError("approved original is outside Company Agent storage")
                if source.stat().st_size > self.service.settings.max_file_bytes:
                    raise ValueError("approved original exceeds the configured size limit")
                shutil.copyfile(source, partial)
                digest = _sha256(partial)
                if digest != document["content_hash"]:
                    raise ValueError("Company Agent original file hash does not match the approved source")
                os.replace(partial, target)
                try:
                    target.chmod(0o600)
                except OSError:
                    pass
                reference_id = document.get("document_id") if source_type == "document" else document["attachment_id"]
                staged.append(StagedKnowledgeFile(reference_id, filename,
                                                  (relative_root / target_name).as_posix(), digest))
            return staged
        except Exception:
            self.cleanup(workspace, execution_id)
            raise

    def cleanup(self, workspace: Workspace, execution_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", execution_id):
            raise ValueError("invalid execution ID for knowledge cleanup")
        inputs_root = workspace.directory("work") / "inputs"
        if inputs_root.is_symlink():
            raise ValueError("knowledge inputs directory cannot be a symbolic link")
        lexical_root = inputs_root / execution_id
        if lexical_root.is_symlink():
            lexical_root.unlink()
            return
        root = self.workspaces.validate_path(workspace.workspace_id, lexical_root)
        if not root.exists():
            return
        if not root.is_dir():
            root.unlink(missing_ok=True)
            return
        shutil.rmtree(root)


def _safe_filename(value: str) -> str:
    name = Path(value).name
    safe = "".join(char for char in name if char.isalnum() or char in " ._-()")[:160].strip(" .")
    return safe or "knowledge-file.txt"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
