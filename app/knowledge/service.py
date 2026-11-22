from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..identity import IdentityRegistry
from ..models import Event, is_direct_source, is_group_source
from ..store import Store
from .config import KnowledgeSettings
from .file_types import TEXT_EXTENSIONS, detect_file
from .anythingllm import AnythingLLMClient

_INTERNAL_INGEST_MARKERS = (
    "/internal",
    "/save-internal",
    "[内部资料]",
    "#内部资料",
    "录入内部",
    "补充内部资料",
    "保存内部资料",
    "存入内部知识库",
    "加入内部知识库",
)


def _is_internal_ingest_message(content: str) -> bool:
    text = (content or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return any(lowered.startswith(marker) or marker in lowered for marker in _INTERNAL_INGEST_MARKERS)


class KnowledgeAccessError(PermissionError):
    pass


class KnowledgeService:
    """Owns document provenance, 3-tier scope checks, short-lived search grants and audit records."""

    def __init__(self, store: Store, settings: KnowledgeSettings, storage_directory: Path,
                 client: AnythingLLMClient | None = None,
                 identities: IdentityRegistry | None = None) -> None:
        self.store = store
        self.settings = settings
        self.identities = identities or IdentityRegistry(store)
        self.storage_directory = storage_directory.resolve()
        self.storage_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.storage_directory.chmod(0o700)
        except OSError:
            pass
        self.client = client or AnythingLLMClient(settings.base_url, settings.api_key,
                                                  timeout=settings.parse_timeout_seconds)
        self.workspace_slug = f"{settings.workspace_prefix}-library"
        self._ingest_lock = threading.Lock()
        self._grant_lock = threading.Lock()

    def ingest_file(self, event: Event, index: int, path: Path, original_filename: str) -> dict[str, Any]:
        scope_type, scope_id = self.scope_for_event(event)
        size = path.stat().st_size
        if not 0 < size <= self.settings.max_file_bytes:
            raise ValueError("file exceeds the configured 500 MiB limit or is empty")
        detected = detect_file(path, original_filename)
        digest = _sha256(path)
        sensitive = detected.sensitive or _contains_secret_assignments(path, original_filename)
        now = _now()
        source_id = f"{event.event_id}:{index}:{digest[:16]}"
        self.client.ensure_workspace(self.workspace_slug)
        original_path = self._store_original(path, digest)
        with self._ingest_lock:
            row = self.store.fetchone(
                "SELECT engine_type,document_id,status,anythingllm_location,original_path "
                "FROM knowledge_documents WHERE content_hash=?", (digest,)
            )
            if row is None:
                with self.store.transaction() as connection:
                    connection.execute(
                        "INSERT INTO knowledge_documents(content_hash,backend_id,document_id,canonical_filename,"
                        "content_type,size_bytes,engine_type,original_path,status,error,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (digest, "anythingllm", digest, detected.filename, detected.content_type, size,
                         "anythingllm", str(original_path), "uploading", None, now, now),
                    )
                document_id, status, location = digest, "uploading", None
            else:
                engine_type, document_id, status, location, stored_path = row
                if engine_type != "anythingllm":
                    # A newly received copy is the migration source for a legacy record.
                    with self.store.transaction() as connection:
                        connection.execute(
                            "UPDATE knowledge_documents SET backend_id='anythingllm',document_id=?,"
                            "canonical_filename=?,content_type=?,size_bytes=?,engine_type='anythingllm',"
                            "original_path=?,status='uploading',error=NULL,updated_at=? WHERE content_hash=?",
                            (digest, detected.filename, detected.content_type, size, str(original_path), now, digest),
                        )
                    document_id, status, location = digest, "uploading", None
                    stored_path = str(original_path)
                if not stored_path or not Path(stored_path).is_file():
                    raise RuntimeError("original file is missing from Company Agent storage; it must be re-ingested")
                original_path = Path(stored_path)
            if status != "ready":
                with self.store.transaction() as connection:
                    connection.execute("UPDATE knowledge_documents SET status='uploading',error=NULL,updated_at=? "
                                       "WHERE content_hash=?", (_now(), digest))
                try:
                    location = self._upload_index_file(path, detected.filename, digest, sensitive)
                    status = "ready"
                    with self.store.transaction() as connection:
                        connection.execute("UPDATE knowledge_documents SET anythingllm_location=?,original_path=?,"
                                           "engine_type='anythingllm',status='ready',error=NULL,updated_at=? "
                                           "WHERE content_hash=?", (location, str(original_path), _now(), digest))
                except Exception as error:
                    with self.store.transaction() as connection:
                        connection.execute("UPDATE knowledge_documents SET status='failed',error=?,updated_at=? "
                                           "WHERE content_hash=?", (type(error).__name__, _now(), digest))
                    raise
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO knowledge_sources(source_id,event_id,content_hash,scope_type,scope_id,"
                    "actor_id,filename,sensitive,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (source_id, event.event_id, digest, scope_type, scope_id, event.actor_id,
                     Path(original_filename).name[:180], int(sensitive), now),
                )
        return {"document_id": document_id, "content_hash": digest, "status": status,
                "filename": Path(original_filename).name[:180], "size_bytes": size,
                "content_type": detected.content_type, "sensitive": sensitive}

    def preserve_original_file(self, event: Event, index: int, path: Path,
                               original_filename: str) -> dict[str, Any]:
        """Durably store an original and its source scope before OCR or indexing starts."""
        scope_type, scope_id = self.scope_for_event(event)
        size = path.stat().st_size
        if not 0 < size <= self.settings.max_file_bytes:
            raise ValueError("file exceeds the configured 500 MiB limit or is empty")
        detected = detect_file(path, original_filename)
        digest = _sha256(path)
        sensitive = detected.sensitive or _contains_secret_assignments(path, original_filename)
        original_path = self._store_original(path, digest)
        now = _now()
        source_id = f"{event.event_id}:{index}:{digest[:16]}"
        with self._ingest_lock, self.store.transaction() as connection:
            row = connection.execute(
                "SELECT engine_type,status,original_path FROM knowledge_documents WHERE content_hash=?",
                (digest,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO knowledge_documents(content_hash,backend_id,document_id,canonical_filename,"
                    "content_type,size_bytes,engine_type,original_path,status,error,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (digest, "anythingllm", digest, detected.filename, detected.content_type, size,
                     "anythingllm", str(original_path), "stored", None, now, now),
                )
                status = "stored"
            else:
                status = row["status"]
                if row["engine_type"] != "anythingllm":
                    connection.execute(
                        "UPDATE knowledge_documents SET backend_id='anythingllm',document_id=?,"
                        "canonical_filename=?,content_type=?,size_bytes=?,engine_type='anythingllm',"
                        "original_path=?,status='stored',error=NULL,updated_at=? WHERE content_hash=?",
                        (digest, detected.filename, detected.content_type, size, str(original_path), now, digest),
                    )
                    status = "stored"
                elif not row["original_path"] or not Path(row["original_path"]).is_file():
                    connection.execute("UPDATE knowledge_documents SET original_path=?,updated_at=? "
                                       "WHERE content_hash=?", (str(original_path), now, digest))
                if status not in {"ready", "stored"}:
                    connection.execute("UPDATE knowledge_documents SET status='stored',error=NULL,updated_at=? "
                                       "WHERE content_hash=?", (now, digest))
                    status = "stored"
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_sources(source_id,event_id,content_hash,scope_type,scope_id,"
                "actor_id,filename,sensitive,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (source_id, event.event_id, digest, scope_type, scope_id, event.actor_id,
                 Path(original_filename).name[:180], int(sensitive), now),
            )
        return {"document_id": digest, "content_hash": digest, "status": status,
                "filename": Path(original_filename).name[:180], "size_bytes": size,
                "content_type": detected.content_type, "sensitive": sensitive,
                "original_path": str(original_path)}

    def _store_original(self, source: Path, digest: str) -> Path:
        directory = self.storage_directory / digest[:2]
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("knowledge original storage cannot contain symbolic links")
        target = directory / digest
        if target.exists():
            if _sha256(target) != digest:
                raise RuntimeError("stored original does not match its content hash")
            return target
        partial = directory / f".{digest}.{secrets.token_hex(8)}.partial"
        try:
            with source.open("rb") as input_file, partial.open("xb") as output:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
            try:
                partial.chmod(0o600)
            except OSError:
                pass
            if _sha256(partial) != digest:
                raise RuntimeError("original changed while it was being stored")
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target

    def _upload_index_file(self, source: Path, filename: str, digest: str, redact_all_values: bool) -> str:
        self.client.ensure_workspace(self.workspace_slug)
        with tempfile.TemporaryDirectory(prefix="company-agent-index-") as temporary:
            indexed_file = source
            parser_extension = Path(filename).suffix.lower()
            if Path(filename).suffix.lower() in TEXT_EXTENSIONS:
                indexed_file = Path(temporary) / f"{Path(filename).stem}.txt"
                if Path(filename).suffix.lower() == ".json" and _contains_secret_assignments(source, filename):
                    indexed_file.write_text("[Sensitive values omitted from searchable JSON copy.]", encoding="utf-8")
                else:
                    _redact_text_file(source, indexed_file, filename=filename,
                                      redact_all_values=redact_all_values)
            return self.client.upload_document(indexed_file, filename, workspaces=[self.workspace_slug],
                                               source_hash=digest, parser_extension=parser_extension if
                                               parser_extension not in TEXT_EXTENSIONS else ".txt")

    def issue_grant(self, event: Event, *, ttl_seconds: int = 3600) -> str:
        if not 60 <= ttl_seconds <= 3600:
            raise ValueError("knowledge grant lifetime must be between 60 and 3600 seconds")
        scope_type, scope_id = self.scope_for_event(event)
        policy = self._access_policy_for_event(event)
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO knowledge_grants(grant_hash,event_id,actor_id,scope_type,scope_id,is_admin,"
                "source_type,source_id,expires_at,call_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self._grant_hash(token), event.event_id, event.actor_id, scope_type, scope_id,
                 int(policy["is_admin"]), event.source_type, event.source_id,
                 (now + timedelta(seconds=ttl_seconds)).isoformat(), 0, now.isoformat()),
            )
        return token

    def search_with_grant(self, token: str, query: str, *, limit: int = 8) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("query must contain 1 to 2000 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        grant = self._consume_grant(token)
        rows = self._authorized_sources(grant)
        allowed = {row["content_hash"]: row for row in rows if row["status"] == "ready"}
        if not allowed:
            results: list[dict[str, Any]] = []
        else:
            chunks = self.client.search(self.workspace_slug, query.strip(), limit=limit)
            results = []
            for chunk in chunks:
                content_hash = _chunk_hash(chunk)
                if content_hash not in allowed:
                    continue
                source = allowed[content_hash]
                metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
                results.append({
                    "document_id": content_hash,
                    "filename": source["filename"],
                    "chunk_id": str(chunk.get("id") or chunk.get("chunk_id") or "")[:200],
                    "location": _chunk_location(metadata),
                    "content": str(chunk.get("text") or chunk.get("content") or "")[:6000],
                    "source": str(metadata.get("title") or metadata.get("url") or source["filename"])[:300],
                    "score": _score(chunk),
                })
                if len(results) >= limit:
                    break
        self._audit_search(grant, query, [item["document_id"] for item in results])
        return {"results": results, "result_count": len(results), "calls_remaining": max(0, 20 - grant["call_count"] - 1)}

    def list_attachments_with_grant(self, token: str, *, query: str = "", limit: int = 30) -> dict[str, Any]:
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError("attachment query must be at most 500 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("attachment limit must be between 1 and 50")
        grant = self._consume_grant(token)
        policy = grant["policy"]
        shared_ids = self._shared_ids_for_policy("attachment", policy)
        rows = self.store.fetchall("SELECT payload FROM attachments ORDER BY created_at DESC")
        matches: list[dict[str, Any]] = []
        needle = query.casefold().strip()
        for row in rows:
            payload = self.store.decode(row["payload"])
            if not self._can_access_record(
                policy,
                record_scope_type=str(payload.get("scope_type", "")),
                record_scope_id=str(payload.get("scope_id", "")),
                target_id=str(payload.get("attachment_id", "")),
                shared_ids=shared_ids,
                sensitive=bool(payload.get("sensitive")),
            ):
                continue
            safe = _public_attachment(payload)
            if needle and needle not in " ".join(str(safe.get(key, "")) for key in
                                                   ("filename", "content_type", "status", "event_id")).casefold():
                continue
            matches.append(safe)
            if len(matches) == limit:
                break
        self._audit_tool_call(grant, "attachments_listed", {"count": len(matches),
                                                             "query_sha256": _sha256_text(query)})
        return {"attachments": matches, "result_count": len(matches),
                "calls_remaining": max(0, 20 - grant["call_count"])}

    def read_attachment_with_grant(self, token: str, attachment_id: str) -> dict[str, Any]:
        if not isinstance(attachment_id, str) or not 1 <= len(attachment_id) <= 200:
            raise ValueError("attachment_id is invalid")
        grant = self._consume_grant(token)
        payload = self._attachment_for_grant(grant, attachment_id)
        safe = _public_attachment(payload)
        path_value = payload.get("original_path")
        if not isinstance(path_value, str) or not path_value:
            self._audit_tool_call(grant, "attachment_read_pending", {"attachment_id": attachment_id})
            return {**safe, "content_available": False, "content": ""}
        if payload.get("sensitive"):
            self._audit_tool_call(grant, "attachment_read_sensitive_blocked", {"attachment_id": attachment_id})
            return {**safe, "content_available": False, "content": "",
                    "content_note": "Sensitive file values are withheld from agent-readable content."}
        path = Path(path_value)
        if not path.is_file():
            raise KnowledgeAccessError("attachment original is unavailable")
        try:
            from ..attachments import extract_text
            content = extract_text(path.read_bytes(), safe["filename"], safe["content_type"], max_chars=60_000)
        except (ValueError, OSError):
            content = ""
        self._audit_tool_call(grant, "attachment_read", {"attachment_id": attachment_id,
                                                          "content_chars": len(content)})
        return {**safe, "content_available": bool(content), "content": content,
                "use_knowledge_search": safe["status"] == "ready" and not bool(content)}

    def read_document_with_grant(self, token: str, document_id: str) -> dict[str, Any]:
        if not isinstance(document_id, str) or not 1 <= len(document_id) <= 200:
            raise ValueError("document_id is invalid")
        grant = self._consume_grant(token)
        event_row = self.store.fetchone(
            "SELECT payload FROM events WHERE event_id=?", (grant["event_id"],)
        )
        if event_row is None:
            raise KnowledgeAccessError("current event is unavailable")
        event = Event.from_dict(self.store.decode(event_row["payload"]))
        documents = self.authorize_documents(event, [document_id])
        document = documents[0]
        content = ""
        if not document["sensitive"] and document.get("original_path"):
            try:
                from ..attachments import extract_text
                content = extract_text(Path(document["original_path"]).read_bytes(), document["filename"],
                                       "application/octet-stream", max_chars=60_000)
            except (ValueError, OSError):
                content = ""
        self._audit_tool_call(grant, "knowledge_document_read", {"document_id": document_id,
                                                                  "content_chars": len(content)})
        return {"document_id": document_id, "filename": document["filename"],
                "content_available": bool(content), "content": content,
                "use_knowledge_search": not bool(content)}

    def conversation_history_with_grant(self, token: str, *, before_event_id: str | None = None,
                                        limit: int = 30) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("history limit must be between 1 and 50")
        if before_event_id is not None and (not isinstance(before_event_id, str)
                                            or not 1 <= len(before_event_id) <= 200):
            raise ValueError("before_event_id is invalid")
        grant = self._consume_grant(token)
        current_row = self.store.fetchone(
            "SELECT payload FROM events WHERE event_id=?", (grant["event_id"],)
        )
        if current_row is None:
            raise KnowledgeAccessError("current event is unavailable")
        current = Event.from_dict(self.store.decode(current_row["payload"]))
        messages: list[Event] = []
        for row in self.store.fetchall("SELECT payload FROM events"):
            item = Event.from_dict(self.store.decode(row["payload"]))
            if (item.conversation_id == current.conversation_id and item.source_type == current.source_type
                    and item.source_id == current.source_id):
                messages.append(item)
        messages.sort(key=lambda item: (_event_timestamp(item.occurred_at), item.event_id))
        if before_event_id:
            cursor = next((index for index, item in enumerate(messages) if item.event_id == before_event_id), None)
            if cursor is None:
                raise KnowledgeAccessError("history cursor is outside the current conversation")
            messages = messages[:cursor]
        has_more = len(messages) > limit
        selected = messages[-limit:]
        result = [{"event_id": item.event_id, "actor_id": item.actor_id, "occurred_at": item.occurred_at,
                   "content": item.content[:12000], "attachments": [_public_attachment(value)
                                                                     for value in item.attachments]}
                  for item in selected]
        self._audit_tool_call(grant, "conversation_history_read", {"message_count": len(result)})
        return {"messages": result, "has_more": has_more,
                "next_before_event_id": selected[0].event_id if has_more and selected else None}

    def authorize_attachments(self, event: Event, attachment_ids: list[str], *,
                              require_original: bool = True) -> list[dict[str, Any]]:
        if len(attachment_ids) > 20 or any(not isinstance(item, str) or not item or len(item) > 200
                                            for item in attachment_ids):
            raise ValueError("at most twenty valid attachment IDs may be attached to one DSH step")
        if len(set(attachment_ids)) != len(attachment_ids):
            raise ValueError("attachment IDs must be unique")
        policy = self._access_policy_for_event(event)
        shared_ids = self._shared_ids_for_policy("attachment", policy)
        result = []
        for attachment_id in attachment_ids:
            row = self.store.fetchone(
                "SELECT payload FROM attachments WHERE attachment_id=?", (attachment_id,)
            )
            if row is None:
                raise KnowledgeAccessError("attachment is outside the current conversation scope")
            payload = self.store.decode(row["payload"])
            sensitive = bool(payload.get("sensitive"))
            if sensitive and not policy["is_admin"]:
                raise KnowledgeAccessError("sensitive attachments require administrator access")
            if not self._can_access_record(
                policy,
                record_scope_type=str(payload.get("scope_type", "")),
                record_scope_id=str(payload.get("scope_id", "")),
                target_id=attachment_id,
                shared_ids=shared_ids,
                sensitive=sensitive,
            ):
                raise KnowledgeAccessError("attachment is outside the current conversation scope")
            original_path = payload.get("original_path")
            if require_original and (not isinstance(original_path, str) or not Path(original_path).is_file()):
                raise KnowledgeAccessError("attachment original has not been stored yet")
            result.append(payload)
        return result

    def authorize_documents(self, event: Event, document_ids: list[str]) -> list[dict[str, Any]]:
        if len(document_ids) > 20 or any(not isinstance(item, str) or not item or len(item) > 200 for item in document_ids):
            raise ValueError("at most twenty valid knowledge document IDs may be attached to one DSH step")
        if len(set(document_ids)) != len(document_ids):
            raise ValueError("knowledge document IDs must be unique")
        policy = self._access_policy_for_event(event)
        shared_ids = self._shared_ids_for_policy("document", policy)
        sensitive_hashes = self._sensitive_hashes()
        docs = []
        for document_id in document_ids:
            is_sensitive = document_id in sensitive_hashes
            if is_sensitive and not policy["is_admin"]:
                raise KnowledgeAccessError("a proposed document is outside the current conversation scope")
            sql = ("SELECT d.content_hash,d.content_hash AS document_id,d.original_path,d.size_bytes,d.status,"
                   "s.filename,s.sensitive,s.scope_type,s.scope_id "
                   "FROM knowledge_documents d JOIN knowledge_sources s ON s.content_hash=d.content_hash "
                   "WHERE d.content_hash=? AND d.engine_type='anythingllm' "
                   "ORDER BY s.created_at DESC")
            rows = self.store.fetchall(sql, (document_id,))
            matched = next(
                (dict(row) for row in rows
                 if row["status"] == "ready" and row["original_path"]
                 and self._can_access_record(
                     policy,
                     record_scope_type=str(row["scope_type"]),
                     record_scope_id=str(row["scope_id"]),
                     target_id=document_id,
                     shared_ids=shared_ids,
                     sensitive=bool(row["sensitive"]) or is_sensitive,
                 )),
                None,
            )
            if matched is None:
                raise KnowledgeAccessError("a proposed document is outside the current conversation scope")
            docs.append(matched)
        return docs

    def source_scope(self, event: Event) -> tuple[str, str]:
        return self.scope_for_event(event)

    def summary(self) -> dict[str, Any]:
        rows = self.store.fetchall(
            "SELECT engine_type,status,COUNT(*) AS count FROM knowledge_documents GROUP BY engine_type,status"
        )
        return {"configured": True, "provider": "anythingllm", "workspace": self.workspace_slug,
                "documents": {row["status"] if row["engine_type"] == "anythingllm"
                              else f"legacy_ragflow_{row['status']}": row["count"] for row in rows},
                "max_file_bytes": self.settings.max_file_bytes}

    def list_documents(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("knowledge document limit must be between 1 and 500")
        rows = self.store.fetchall(
            "SELECT d.content_hash AS document_id,d.content_hash,d.canonical_filename,d.content_type,d.size_bytes,"
            "CASE WHEN d.engine_type='anythingllm' THEN d.status ELSE 'legacy_ragflow' END AS status,d.error,"
            "d.created_at,d.updated_at,COUNT(s.source_id) AS source_count "
            "FROM knowledge_documents d LEFT JOIN knowledge_sources s ON s.content_hash=d.content_hash "
            "GROUP BY d.content_hash ORDER BY d.created_at DESC LIMIT ?", (limit,)
        )
        docs = [dict(row) for row in rows]
        if not docs:
            return docs
        hashes = [doc["content_hash"] for doc in docs]
        placeholders = ",".join("?" for _ in hashes)
        source_rows = self.store.fetchall(
            f"SELECT DISTINCT content_hash,scope_type,scope_id FROM knowledge_sources WHERE content_hash IN ({placeholders})",
            hashes,
        )
        scopes_by_hash: dict[str, list[dict[str, str]]] = {}
        for row in source_rows:
            scopes_by_hash.setdefault(row["content_hash"], []).append({
                "scope_type": row["scope_type"], "scope_id": row["scope_id"],
            })
        share_rows = self.store.fetchall(
            f"SELECT share_id,target_id,target_scope_type,target_scope_id,created_by,note,created_at "
            f"FROM knowledge_shares WHERE target_kind='document' AND target_id IN ({placeholders})",
            hashes,
        )
        shares_by_hash: dict[str, list[dict[str, Any]]] = {}
        for row in share_rows:
            shares_by_hash.setdefault(row["target_id"], []).append(dict(row))
        for doc in docs:
            doc["scopes"] = scopes_by_hash.get(doc["content_hash"], [])
            doc["shares"] = shares_by_hash.get(doc["content_hash"], [])
        return docs

    def is_downward_compat_enabled(self) -> bool:
        row = self.store.fetchone(
            "SELECT payload FROM settings WHERE setting_key='downward_compatibility_enabled'"
        )
        if row is None:
            return True
        value = self.store.decode(row["payload"]).get("value", True)
        return bool(value)

    def share_item(self, *, target_kind: str, target_id: str, target_scope_type: str,
                   target_scope_id: str, created_by: str, note: str = "") -> dict[str, Any]:
        if target_kind not in {"document", "attachment"}:
            raise ValueError("target_kind must be document or attachment")
        if target_scope_type not in {"internal", "group", "direct"}:
            raise ValueError("target_scope_type must be internal, group, or direct")
        cleaned_id = (target_id or "").strip()
        cleaned_scope_id = (target_scope_id or "").strip()
        if not cleaned_id or not cleaned_scope_id:
            raise ValueError("target_id and target_scope_id are required")
        share_id = f"share-{secrets.token_hex(12)}"
        now = _now()
        with self.store.transaction() as connection:
            if target_kind == "document":
                if not connection.execute("SELECT 1 FROM knowledge_documents WHERE content_hash=?", (cleaned_id,)).fetchone():
                    raise ValueError(f"document not found: {cleaned_id}")
            else:
                if not connection.execute("SELECT 1 FROM attachments WHERE attachment_id=?", (cleaned_id,)).fetchone():
                    raise ValueError(f"attachment not found: {cleaned_id}")
            connection.execute(
                "INSERT INTO knowledge_shares(share_id,target_kind,target_id,target_scope_type,"
                "target_scope_id,created_by,note,created_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(target_kind,target_id,target_scope_type,target_scope_id) DO UPDATE "
                "SET note=excluded.note,created_by=excluded.created_by",
                (share_id, target_kind, cleaned_id, target_scope_type, cleaned_scope_id,
                 created_by.strip()[:120] or "admin", note.strip()[:240], now),
            )
            row = connection.execute(
                "SELECT share_id,target_kind,target_id,target_scope_type,target_scope_id,"
                "created_by,note,created_at FROM knowledge_shares "
                "WHERE target_kind=? AND target_id=? AND target_scope_type=? AND target_scope_id=?",
                (target_kind, cleaned_id, target_scope_type, cleaned_scope_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO audit(entity_type,entity_id,action,payload,created_at) VALUES (?,?,?,?,?)",
                ("knowledge_share", row["share_id"], "shared", self.store.encode(dict(row)), now),
            )
            return dict(row)

    def unshare_item(self, share_id: str) -> bool:
        now = _now()
        with self.store.transaction() as connection:
            row = connection.execute("SELECT 1 FROM knowledge_shares WHERE share_id=?", (share_id,)).fetchone()
            if row is None:
                return False
            connection.execute("DELETE FROM knowledge_shares WHERE share_id=?", (share_id,))
            connection.execute(
                "INSERT INTO audit(entity_type,entity_id,action,payload,created_at) VALUES (?,?,?,?,?)",
                ("knowledge_share", share_id, "unshared", "{}", now),
            )
            return True

    def list_shares(self, *, target_id: str | None = None) -> list[dict[str, Any]]:
        if target_id:
            rows = self.store.fetchall(
                "SELECT share_id,target_kind,target_id,target_scope_type,target_scope_id,created_by,note,created_at "
                "FROM knowledge_shares WHERE target_id=? ORDER BY created_at DESC",
                (target_id,),
            )
        else:
            rows = self.store.fetchall(
                "SELECT share_id,target_kind,target_id,target_scope_type,target_scope_id,created_by,note,created_at "
                "FROM knowledge_shares ORDER BY created_at DESC LIMIT 500"
            )
        return [dict(row) for row in rows]

    def ingest_internal_text(self, *, actor_id: str, title: str, text: str,
                             event_id: str | None = None) -> dict[str, Any]:
        body = (text or "").strip()
        if not body:
            raise ValueError("internal note text cannot be empty")
        cleaned_title = re.sub(r'[\\/:*?"<>|\s]+', "-", (title or "internal-note").strip())[:60]
        cleaned_title = cleaned_title.lstrip(".") or "internal-note"
        synthetic_event = Event(
            event_id=event_id or f"internal-note-{secrets.token_hex(8)}",
            source_type="internal",
            source_id="company",
            conversation_id="internal:company",
            actor_id=actor_id,
            occurred_at=_now(),
            content=body[:2000],
            attachments=[],
            capabilities=["dsh.task"],
        )
        with tempfile.TemporaryDirectory(prefix="company-agent-internal-note-") as temp_dir:
            note_path = Path(temp_dir) / f"{cleaned_title}.md"
            note_path.write_text(body, encoding="utf-8")
            return self.ingest_file(synthetic_event, 1, note_path, f"{cleaned_title}.md")

    def scope_for_event(self, event: Event) -> tuple[str, str]:
        if is_group_source(event.source_type):
            group = self.identities.get_group(event.source_type, event.source_id)
            if group is not None and group.tier == "internal":
                return "internal", "company"
            return "group", event.source_id
        if is_direct_source(event.source_type):
            role = self.identities.user_role(event.actor_id, self.settings.admin_user_ids)
            if role in {"admin", "employee"} and _is_internal_ingest_message(event.content):
                return "internal", "company"
            return "direct", event.conversation_id
        if event.source_type in {"internal", "admin", "console", "manual"}:
            return "internal", "company"
        raise ValueError("knowledge scope is only defined for IM group and direct conversations")

    def _access_policy_for_event(self, event: Event) -> dict[str, Any]:
        scope_type, scope_id = self.scope_for_event(event)
        return self._build_access_policy(
            source_type=event.source_type,
            source_id=event.source_id,
            conversation_id=event.conversation_id,
            scope_type=scope_type,
            scope_id=scope_id,
            actor_id=event.actor_id,
        )

    def _build_access_policy(self, *, source_type: str, source_id: str, conversation_id: str,
                             scope_type: str, scope_id: str, actor_id: str) -> dict[str, Any]:
        role = self.identities.user_role(actor_id, self.settings.admin_user_ids)
        global_compat = self.is_downward_compat_enabled()
        own_scopes: set[tuple[str, str]] = {(scope_type, scope_id)}
        if is_group_source(source_type):
            group = self.identities.get_group(source_type, source_id)
            if group is not None and group.tier == "internal":
                own_scopes.add(("internal", "company"))
                own_scopes.add(("group", source_id))
                if group.downward_compat == "on":
                    compat = True
                elif group.downward_compat == "off":
                    compat = False
                else:
                    compat = global_compat
                return {
                    "tier": "internal",
                    "role": role,
                    "is_admin": False,
                    "can_read_internal": True,
                    "downward_compat": compat,
                    "own_scopes": own_scopes,
                }
            # Location Ceiling Invariant: L1 client groups stay locked to their own group scope
            # regardless of whether the speaker is an employee or admin.
            return {
                "tier": "client_group",
                "role": role,
                "is_admin": False,
                "can_read_internal": False,
                "downward_compat": False,
                "own_scopes": {("group", source_id)},
            }
        if is_direct_source(source_type):
            if conversation_id:
                own_scopes.add(("direct", conversation_id))
            if role == "admin":
                own_scopes.add(("internal", "company"))
                return {
                    "tier": "admin",
                    "role": role,
                    "is_admin": True,
                    "can_read_internal": True,
                    "downward_compat": True,
                    "own_scopes": own_scopes,
                }
            if role == "employee":
                own_scopes.add(("internal", "company"))
                return {
                    "tier": "internal",
                    "role": role,
                    "is_admin": False,
                    "can_read_internal": True,
                    "downward_compat": global_compat,
                    "own_scopes": own_scopes,
                }
            return {
                "tier": "direct",
                "role": role,
                "is_admin": False,
                "can_read_internal": False,
                "downward_compat": False,
                "own_scopes": own_scopes,
            }
        # internal / admin / console / manual
        if role == "admin":
            own_scopes.add(("internal", "company"))
            return {
                "tier": "admin",
                "role": role,
                "is_admin": True,
                "can_read_internal": True,
                "downward_compat": True,
                "own_scopes": own_scopes,
            }
        if role == "employee":
            own_scopes.add(("internal", "company"))
            return {
                "tier": "internal",
                "role": role,
                "is_admin": False,
                "can_read_internal": True,
                "downward_compat": global_compat,
                "own_scopes": own_scopes,
            }
        return {
            "tier": "direct",
            "role": role,
            "is_admin": False,
            "can_read_internal": False,
            "downward_compat": False,
            "own_scopes": own_scopes,
        }

    def _group_tier_sets(self) -> tuple[set[str], set[str]]:
        internal_ids: set[str] = set()
        disabled_client_ids: set[str] = set()
        for group in self.identities.list_groups():
            if group.tier == "internal":
                internal_ids.add(group.source_id)
            elif group.downward_compat == "off":
                disabled_client_ids.add(group.source_id)
        return internal_ids, disabled_client_ids

    def _shared_ids_for_policy(self, target_kind: str, policy: dict[str, Any]) -> set[str]:
        scopes = sorted(policy["own_scopes"])
        if not scopes:
            return set()
        clauses = " OR ".join("(target_scope_type=? AND target_scope_id=?)" for _ in scopes)
        params: list[str] = [target_kind]
        for s_type, s_id in scopes:
            params.extend((s_type, s_id))
        rows = self.store.fetchall(
            f"SELECT target_id FROM knowledge_shares WHERE target_kind=? AND ({clauses})",
            params,
        )
        return {row[0] for row in rows}

    def _sensitive_hashes(self) -> set[str]:
        rows = self.store.fetchall(
            "SELECT DISTINCT content_hash FROM knowledge_sources WHERE sensitive=1"
        )
        return {row[0] for row in rows}

    def _can_access_record(self, policy: dict[str, Any], *, record_scope_type: str,
                           record_scope_id: str, target_id: str, shared_ids: set[str],
                           sensitive: bool) -> bool:
        if sensitive:
            return bool(policy["is_admin"])
        if policy["is_admin"]:
            return True
        if (record_scope_type, record_scope_id) in policy["own_scopes"]:
            return True
        internal_ids, disabled_client_ids = self._group_tier_sets()
        if policy["can_read_internal"]:
            if record_scope_type == "internal":
                return True
            if record_scope_type == "group" and record_scope_id in internal_ids:
                return True
            if policy["downward_compat"] and record_scope_type in {"group", "direct"}:
                if record_scope_type == "group" and record_scope_id in disabled_client_ids:
                    pass
                else:
                    return True
        return target_id in shared_ids

    def _consume_grant(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise KnowledgeAccessError("invalid or expired knowledge grant")
        digest = self._grant_hash(token)
        now = _now()
        with self._grant_lock, self.store.transaction() as connection:
            updated = connection.execute(
                "UPDATE knowledge_grants SET call_count=call_count+1 "
                "WHERE grant_hash=? AND expires_at>? AND call_count<20", (digest, now)
            )
            if updated.rowcount != 1:
                raise KnowledgeAccessError("invalid, expired, or exhausted knowledge grant")
            row = connection.execute(
                "SELECT event_id,actor_id,scope_type,scope_id,is_admin,source_type,source_id,expires_at,call_count "
                "FROM knowledge_grants WHERE grant_hash=?", (digest,)
            ).fetchone()
        grant = dict(row)
        source_type = str(grant.get("source_type") or "")
        source_id = str(grant.get("source_id") or "")
        conversation_id = source_id
        event_row = self.store.fetchone(
            "SELECT payload FROM events WHERE event_id=?", (grant["event_id"],)
        )
        if event_row is not None:
            event_payload = self.store.decode(event_row["payload"])
            source_type = source_type or str(event_payload.get("source_type") or "")
            source_id = source_id or str(event_payload.get("source_id") or "")
            conversation_id = str(event_payload.get("conversation_id") or "") or source_id
        if not source_type:
            scope_type = grant["scope_type"]
            scope_id = grant["scope_id"]
            if scope_type == "group":
                source_type, source_id, conversation_id = "qq.group", scope_id, scope_id
            elif scope_type == "direct":
                source_type, source_id, conversation_id = "qq.c2c", scope_id, scope_id
            else:
                source_type, source_id, conversation_id = "internal", scope_id, scope_id
        policy = self._build_access_policy(
            source_type=source_type,
            source_id=source_id,
            conversation_id=conversation_id,
            scope_type=grant["scope_type"],
            scope_id=grant["scope_id"],
            actor_id=grant["actor_id"],
        )
        # Preserve stricter Location Ceiling recorded at grant issuance if applicable.
        if not grant["is_admin"]:
            policy["is_admin"] = False
        grant["policy"] = policy
        return grant

    def _grant_hash(self, token: str) -> str:
        return hmac.new(self.settings.grant_secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _attachment_for_grant(self, grant: dict[str, Any], attachment_id: str) -> dict[str, Any]:
        row = self.store.fetchone("SELECT payload FROM attachments WHERE attachment_id=?",
                                  (attachment_id,))
        if row is None:
            raise KnowledgeAccessError("attachment is outside the current conversation scope")
        payload = self.store.decode(row["payload"])
        policy = grant["policy"]
        shared_ids = self._shared_ids_for_policy("attachment", policy)
        if not self._can_access_record(
            policy,
            record_scope_type=str(payload.get("scope_type", "")),
            record_scope_id=str(payload.get("scope_id", "")),
            target_id=attachment_id,
            shared_ids=shared_ids,
            sensitive=bool(payload.get("sensitive")),
        ):
            raise KnowledgeAccessError("attachment is outside the current conversation scope")
        return payload

    def _audit_tool_call(self, grant: dict[str, Any], action: str, payload: dict[str, Any]) -> None:
        value = {"event_id": grant["event_id"], "scope_type": grant["scope_type"],
                 "scope_id": grant["scope_id"], **payload}
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO audit(entity_type,entity_id,action,payload,created_at) "
                               "VALUES (?,?,?,?,?)",
                               ("knowledge_tool", grant["event_id"], action, self.store.encode(value), _now()))

    def _authorized_sources(self, grant: dict[str, Any]) -> list[dict[str, Any]]:
        policy = grant["policy"]
        shared_ids = self._shared_ids_for_policy("document", policy)
        sensitive_hashes = self._sensitive_hashes()
        query = ("SELECT d.content_hash AS document_id,d.status,d.content_hash,s.filename,"
                 "s.sensitive,s.scope_type,s.scope_id "
                 "FROM knowledge_documents d JOIN knowledge_sources s ON s.content_hash=d.content_hash "
                 "WHERE d.engine_type='anythingllm' ORDER BY s.created_at DESC")
        rows = self.store.fetchall(query)
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            doc_id = row["document_id"]
            if doc_id in unique:
                continue
            is_sensitive = bool(row["sensitive"]) or (doc_id in sensitive_hashes)
            if not self._can_access_record(
                policy,
                record_scope_type=str(row["scope_type"]),
                record_scope_id=str(row["scope_id"]),
                target_id=doc_id,
                shared_ids=shared_ids,
                sensitive=is_sensitive,
            ):
                continue
            unique[doc_id] = dict(row)
        return list(unique.values())

    def _audit_search(self, grant: dict[str, Any], query: str, document_ids: list[str]) -> None:
        now = _now()
        payload = {"event_id": grant["event_id"], "scope_type": grant["scope_type"],
                   "scope_id": grant["scope_id"], "query_sha256": _sha256_text(query),
                   "result_document_ids": sorted(set(document_ids))}
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO audit(entity_type,entity_id,action,payload,created_at) VALUES (?,?,?,?,?)",
                               ("knowledge_search", grant["event_id"], "searched", self.store.encode(payload), now))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _public_attachment(payload: dict[str, Any]) -> dict[str, Any]:
    fields = ("attachment_id", "event_id", "filename", "content_type", "occurred_at", "received_at", "status",
              "size_bytes", "content_hash", "document_id", "sensitive", "failure_category")
    return {key: payload[key] for key in fields if key in payload}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


def _chunk_location(chunk: dict[str, Any]) -> str:
    for key in ("position_int", "page_num", "page", "position"):
        if chunk.get(key) is not None:
            return str(chunk[key])[:120]
    return ""


def _contains_secret_assignments(path: Path, filename: str) -> bool:
    if Path(filename).suffix.lower() not in TEXT_EXTENSIONS and Path(filename).name.lower() != ".env":
        return False
    secret_key = re.compile(r"(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|AUTH|DSN)", re.I)
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            return any(("=" in line or ":" in line) and secret_key.search(re.split(r"[=:]", line, maxsplit=1)[0])
                       for line in source)
    except UnicodeDecodeError:
        return False


def _redact_text_file(source_path: Path, target_path: Path, *, filename: str, redact_all_values: bool) -> None:
    assignment = re.compile(r"^(\s*(?:export\s+)?[\"']?([A-Za-z_][A-Za-z0-9_.-]*)[\"']?\s*[=:]\s*).*$", re.I)
    secret_key = re.compile(r"(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|AUTH|DSN)", re.I)
    redacted = 0
    with source_path.open("r", encoding="utf-8-sig", newline="") as source, target_path.open(
            "w", encoding="utf-8", newline="") as target:
        for line in source:
            match = assignment.match(line.rstrip("\r\n"))
            if match and (redact_all_values or secret_key.search(match.group(2))):
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                target.write(match.group(1) + "[REDACTED]" + ending)
                redacted += 1
            else:
                target.write(line)
    if not redact_all_values and _contains_secret_assignments(source_path, filename) and not redacted:
        target_path.unlink(missing_ok=True)
        raise ValueError("sensitive text format could not be safely redacted for indexing")


def _chunk_hash(chunk: dict[str, Any]) -> str:
    metadata = chunk.get("metadata")
    values = list(metadata.values()) if isinstance(metadata, dict) else []
    values.extend(chunk.get(key) for key in ("source", "title", "url", "docSource"))
    for value in values:
        if not isinstance(value, str):
            continue
        match = re.search(r"(?:ca-|company-agent-sha256:)([0-9a-f]{64})(?:[-.]|$)", value)
        if match:
            return match.group(1)
    return ""


def _score(chunk: dict[str, Any]) -> float | None:
    for key in ("similarity", "score", "vector_similarity", "distance"):
        value = chunk.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None
