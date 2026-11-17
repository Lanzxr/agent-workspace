from __future__ import annotations

import sqlite3
import hashlib
import tempfile
import unittest
from pathlib import Path

from app.knowledge.config import KnowledgeSettings
from app.knowledge.service import KnowledgeService
from app.models import Event
from app.store import Store


class KnowledgeMigrationTests(unittest.TestCase):
    def test_legacy_ragflow_rows_are_preserved_and_marked_as_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "control.sqlite"
            original = Path(directory) / "old.pdf"
            original.write_bytes(b"%PDF-1.4\nlegacy file")
            content_hash = hashlib.sha256(original.read_bytes()).hexdigest()
            connection = sqlite3.connect(database)
            connection.execute("""CREATE TABLE knowledge_documents (
                content_hash TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, document_id TEXT NOT NULL UNIQUE,
                canonical_filename TEXT NOT NULL, content_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            connection.execute("INSERT INTO knowledge_documents VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (content_hash, "legacy-dataset", "legacy-doc", "old.pdf", "application/pdf",
                                123, "ready", None, "created", "updated"))
            connection.commit()
            connection.close()

            store = Store(database)
            try:
                row = store.connection.execute("SELECT * FROM knowledge_documents").fetchone()
                self.assertEqual(row["backend_id"], "legacy-dataset")
                self.assertEqual(row["document_id"], "legacy-doc")
                self.assertEqual(row["engine_type"], "legacy_ragflow")
                self.assertIsNone(row["original_path"])
                self.assertEqual(row["status"], "ready")
                self.assertNotIn("dataset_id", {column[1] for column in
                                                 store.connection.execute("PRAGMA table_info(knowledge_documents)")})

                store.connection.execute(
                    "INSERT INTO knowledge_sources(source_id,event_id,content_hash,scope_type,scope_id,actor_id,"
                    "filename,sensitive,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("legacy-source", "old-event", content_hash, "qq.group", "group-1", "user-1", "old.pdf", 0,
                     "created"),
                )

                class Client:
                    def ensure_workspace(self, slug: str) -> str:
                        return slug

                    def upload_document(self, path: Path, filename: str, *, workspaces: list[str], source_hash: str,
                                        parser_extension: str | None = None) -> str:
                        return f"custom-documents/{source_hash}.pdf"

                    def search(self, *_args, **_kwargs):
                        return []

                service = KnowledgeService(store, KnowledgeSettings("http://127.0.0.1", "key", "g" * 40),
                                           Path(directory) / "originals", Client())
                event = Event("new-event", "qq.group", "group-1", "group-1", "user-1",
                              "2026-11-17T00:00:00+00:00", "reingest", [], [])
                service.ingest_file(event, 1, original, "old.pdf")
                migrated = store.connection.execute("SELECT engine_type,status,backend_id FROM knowledge_documents "
                                                     "WHERE content_hash=?", (content_hash,)).fetchone()
                self.assertEqual(tuple(migrated), ("anythingllm", "ready", "anythingllm"))
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0], 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
