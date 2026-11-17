from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._transaction_lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_items (
                work_item_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assistant_runs (
                event_id TEXT PRIMARY KEY,
                work_item_id TEXT,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS assistant_runs_work_item_idx ON assistant_runs(work_item_id);
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                memory_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                work_item_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT PRIMARY KEY,
                work_item_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS executions (
                execution_id TEXT PRIMARY KEY,
                work_item_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_results (
                execution_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publication_decisions (
                decision_id TEXT PRIMARY KEY,
                work_item_id TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                outbox_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS qq_reply_sequences (
                conversation_id TEXT NOT NULL,
                reply_message_id TEXT NOT NULL,
                last_sequence INTEGER NOT NULL,
                PRIMARY KEY(conversation_id, reply_message_id)
            );
            CREATE TABLE IF NOT EXISTS audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS managed_users (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                time_zone TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS identity_bindings (
                binding_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                account_id TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                user_id TEXT NOT NULL REFERENCES managed_users(user_id),
                created_at TEXT NOT NULL,
                UNIQUE(provider, account_id, scope_id, external_id)
            );
            CREATE INDEX IF NOT EXISTS identity_bindings_user_idx ON identity_bindings(user_id);
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                schedule_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                due_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS scheduled_tasks_due_idx ON scheduled_tasks(status, due_at);
            CREATE TABLE IF NOT EXISTS settings (
                setting_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attachments (
                attachment_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS attachments_event_idx ON attachments(event_id);
            CREATE TABLE IF NOT EXISTS knowledge_documents (
                content_hash TEXT PRIMARY KEY,
                backend_id TEXT NOT NULL,
                document_id TEXT NOT NULL UNIQUE,
                canonical_filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                engine_type TEXT NOT NULL DEFAULT 'anythingllm',
                anythingllm_location TEXT,
                original_path TEXT,
                status TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS knowledge_documents_status_idx ON knowledge_documents(status);
            CREATE TABLE IF NOT EXISTS knowledge_sources (
                source_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                content_hash TEXT NOT NULL REFERENCES knowledge_documents(content_hash),
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                sensitive INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(event_id, filename, content_hash)
            );
            CREATE INDEX IF NOT EXISTS knowledge_sources_scope_idx ON knowledge_sources(scope_type, scope_id);
            CREATE INDEX IF NOT EXISTS knowledge_sources_event_idx ON knowledge_sources(event_id);
            CREATE TABLE IF NOT EXISTS knowledge_grants (
                grant_hash TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL,
                call_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS knowledge_grants_expiry_idx ON knowledge_grants(expires_at);
            """
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(knowledge_documents)")}
        if "dataset_id" in columns and "backend_id" not in columns:
            self.connection.execute("ALTER TABLE knowledge_documents RENAME COLUMN dataset_id TO backend_id")
            columns.remove("dataset_id")
            columns.add("backend_id")
        migrations = {
            "engine_type": "TEXT NOT NULL DEFAULT 'legacy_ragflow'",
            "anythingllm_location": "TEXT",
            "original_path": "TEXT",
        }
        for name, declaration in migrations.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE knowledge_documents ADD COLUMN {name} {declaration}")
        publication_columns = {
            row[1]: row for row in self.connection.execute("PRAGMA table_info(publication_decisions)")
        }
        if publication_columns.get("work_item_id") and publication_columns["work_item_id"][3]:
            with self.transaction() as connection:
                connection.execute("ALTER TABLE publication_decisions RENAME TO publication_decisions_legacy")
                connection.execute(
                    "CREATE TABLE publication_decisions ("
                    "decision_id TEXT PRIMARY KEY, work_item_id TEXT, idempotency_key TEXT NOT NULL UNIQUE, "
                    "payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO publication_decisions(decision_id, work_item_id, idempotency_key, payload, created_at) "
                    "SELECT decision_id, work_item_id, idempotency_key, payload, created_at "
                    "FROM publication_decisions_legacy"
                )
                connection.execute("DROP TABLE publication_decisions_legacy")
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._transaction_lock:
            try:
                yield self.connection
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    @staticmethod
    def encode(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def decode(value: str) -> dict[str, Any]:
        return json.loads(value)

    def close(self) -> None:
        self.connection.close()

