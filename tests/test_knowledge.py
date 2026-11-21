from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.knowledge.config import KnowledgeSettings, MAX_KNOWLEDGE_FILE_BYTES
from app.knowledge.delivery import KnowledgeDelivery
from app.knowledge.http_api import KnowledgeQueryServer, make_handler
from app.knowledge.service import KnowledgeAccessError, KnowledgeService
from app.models import Event
from app.store import Store
from app.workspace import WorkspaceManager


class FakeAnythingLLM:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.uploads = 0
        self.search_calls: list[tuple[str, str]] = []
        self.fail_upload = False
        self.search_result_text: str | None = None

    def ensure_workspace(self, _slug: str) -> str:
        return _slug

    def upload_document(self, path: Path, filename: str, *, workspaces: list[str], source_hash: str,
                        parser_extension: str | None = None) -> str:
        if self.fail_upload:
            raise RuntimeError("upload unavailable")
        if source_hash not in self.files:
            self.uploads += 1
        self.files[source_hash] = path.read_bytes()
        self.upload_extensions = getattr(self, "upload_extensions", []) + [parser_extension]
        return f"custom-documents/{source_hash}.txt"

    def search(self, workspace_slug: str, query: str, *, limit: int = 8) -> list[dict]:
        self.search_calls.append((workspace_slug, query))
        return ([{"id": "chunk-1", "text": self.search_result_text or value.decode("utf-8", errors="replace"),
                  "metadata": {"docSource": f"custom-documents/ca-{digest}-{digest}.txt", "title": f"ca-{digest}.txt",
                               "page": 1}, "score": 0.9} for digest, value in list(self.files.items())[:limit]]
                + [{"id": "foreign", "text": "must be filtered", "metadata": {"docSource": "foreign"}}])


class KnowledgeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "control.sqlite")
        self.client = FakeAnythingLLM()
        settings = KnowledgeSettings("http://127.0.0.1:3001", "anythingllm-key", "g" * 40,
                                     frozenset({"administrator"}), parse_timeout_seconds=1)
        self.service = KnowledgeService(self.store, settings, self.root / "knowledge-originals", self.client)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def event(event_id: str, source_type: str = "qq.group", source_id: str = "group-1",
              actor_id: str = "user-1") -> Event:
        return Event(event_id, source_type, source_id, source_id, actor_id,
                     "2026-11-16T00:00:00+00:00", "Please inspect the file", [], ["dsh.task"])

    def ingest(self, event: Event, filename: str, content: str) -> dict:
        path = self.root / f"{event.event_id.replace(':', '-')}-{filename.lstrip('.')}"
        path.write_text(content, encoding="utf-8")
        return self.service.ingest_file(event, 1, path, filename)

    def test_hash_deduplicates_storage_but_preserves_group_sources_and_enforces_scope(self) -> None:
        first = self.ingest(self.event("g1-message"), "guide.md", "group one handbook")
        other_group = self.event("g2-message", source_id="group-2", actor_id="user-2")
        duplicate = self.ingest(other_group, "guide.md", "group one handbook")
        self.assertEqual(first["document_id"], duplicate["document_id"])
        self.assertEqual(self.client.uploads, 1)

        group_one_grant = self.service.issue_grant(self.event("g1-query"))
        group_two_grant = self.service.issue_grant(self.event("g2-query", source_id="group-2", actor_id="user-2"))
        self.assertEqual(self.service.search_with_grant(group_one_grant, "handbook")["result_count"], 1)
        self.assertEqual(self.service.search_with_grant(group_two_grant, "handbook")["result_count"], 1)
        with self.assertRaises(KnowledgeAccessError):
            self.service.authorize_documents(self.event("g3-query", source_id="group-3"),
                                             [first["document_id"]])
        self.assertNotIn("foreign-document", json.dumps(self.service.search_with_grant(group_one_grant, "handbook")))
        self.assertEqual(self.service.store.connection.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0], 2)

    def test_text_config_is_uploaded_as_plain_text_and_source_filename_is_retained(self) -> None:
        result = self.ingest(self.event("config-source"), "service.conf", "port=8080\n")
        self.assertEqual(result["filename"], "service.conf")
        self.assertEqual(self.client.upload_extensions, [".txt"])
        self.assertIn(b"port=8080", next(iter(self.client.files.values())))

    def test_png_is_stored_and_uploaded_for_anythingllm_ocr(self) -> None:
        path = self.root / "chinese.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"synthetic image payload")
        result = self.service.ingest_file(self.event("image-source"), 1, path, "中文图片.jpg")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["content_type"], "image/png")
        self.assertEqual(self.client.upload_extensions, [".png"])
        self.assertEqual(self.client.files[result["content_hash"]], path.read_bytes())
        self.client.search_result_text = "扫描结论：设备数量为 37 台。"
        token = self.service.issue_grant(self.event("image-query"))
        retrieved = self.service.search_with_grant(token, "设备数量")
        self.assertIn("37 台", retrieved["results"][0]["content"])
        self.assertEqual(retrieved["results"][0]["document_id"], result["document_id"])

    def test_scanned_pdf_uses_anythingllm_parser_and_upload_failure_is_failed(self) -> None:
        path = self.root / "scan.pdf"
        path.write_bytes(b"%PDF-1.7\nsynthetic scanned pages")
        result = self.service.ingest_file(self.event("scan-source"), 1, path, "中文扫描.pdf")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.client.upload_extensions, [".pdf"])

        failed_path = self.root / "scan-failed.pdf"
        failed_path.write_bytes(b"%PDF-1.7\nocr failure")
        self.client.fail_upload = True
        with self.assertRaises(RuntimeError):
            self.service.ingest_file(self.event("scan-failed"), 1, failed_path, "failed-scan.pdf")
        row = self.store.connection.execute(
            "SELECT status FROM knowledge_documents WHERE canonical_filename=?", ("failed-scan.pdf",)
        ).fetchone()
        self.assertEqual(row[0], "failed")

    def test_direct_conversation_scope_is_separate_from_group_scope_and_admin_sees_all(self) -> None:
        group_doc = self.ingest(self.event("group-file"), "group.txt", "group only evidence")
        direct = self.event("direct-file", source_type="qq.c2c", source_id="openid-alice", actor_id="alice")
        direct_doc = self.ingest(direct, "direct.txt", "private direct evidence")
        direct_token = self.service.issue_grant(self.event("direct-query", "qq.c2c", "openid-alice", "alice"))
        self.assertEqual(self.service.search_with_grant(direct_token, "private")["results"][0]["document_id"],
                         direct_doc["document_id"])
        with self.assertRaises(KnowledgeAccessError):
            self.service.authorize_documents(direct, [group_doc["document_id"]])
        admin = self.event("admin-query", source_type="qq.c2c", source_id="openid-admin", actor_id="administrator")
        admin_token = self.service.issue_grant(admin)
        results = self.service.search_with_grant(admin_token, "evidence")["results"]
        self.assertEqual({item["document_id"] for item in results}, {group_doc["document_id"], direct_doc["document_id"]})

    def test_env_is_admin_only_even_when_same_content_has_a_normal_filename(self) -> None:
        env_event = self.event("env-upload")
        secret = self.ingest(env_event, ".env", "API_TOKEN=do-not-expose")
        indexed = next(iter(self.client.files.values()))
        self.assertIn(b"[REDACTED]", indexed)
        self.assertNotIn(b"do-not-expose", indexed)
        duplicate = self.ingest(self.event("normal-copy", source_id="group-2", actor_id="user-2"),
                                "notes.txt", "API_TOKEN=do-not-expose")
        self.assertEqual(secret["document_id"], duplicate["document_id"])
        grant = self.service.issue_grant(self.event("ordinary-query", source_id="group-2", actor_id="user-2"))
        self.assertEqual(self.service.search_with_grant(grant, "API token")["result_count"], 0)
        with self.assertRaises(KnowledgeAccessError):
            self.service.authorize_documents(self.event("ordinary-plan", source_id="group-2", actor_id="user-2"),
                                             [secret["document_id"]])

    def test_dotenv_variant_is_also_marked_sensitive(self) -> None:
        secret = self.ingest(self.event("dotenv-variant"), ".env.production", "API_TOKEN=private")
        self.assertTrue(secret["sensitive"])
        grant = self.service.issue_grant(self.event("dotenv-query"))
        self.assertEqual(self.service.search_with_grant(grant, "API token")["result_count"], 0)

    def test_non_utf8_or_binary_bytes_after_the_sample_are_rejected(self) -> None:
        for event_id, content in (("late-invalid-utf8", b"a" * 8192 + b"\xff"),
                                  ("late-null-byte", b"a" * 8192 + b"\x00")):
            path = self.root / f"{event_id}.txt"
            path.write_bytes(content)
            with self.subTest(event_id=event_id), self.assertRaises(ValueError):
                self.service.ingest_file(self.event(event_id), 1, path, "notes.txt")

    def test_failed_parsing_retries_without_duplicate_upload_and_500_mib_limit_is_hard_capped(self) -> None:
        self.client.fail_upload = True
        event = self.event("retry")
        path = self.root / "retry.txt"
        path.write_text("retry me", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.service.ingest_file(event, 1, path, "retry.txt")
        self.client.fail_upload = False
        result = self.service.ingest_file(event, 1, path, "retry.txt")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(self.client.uploads, 1)
        self.assertEqual(self.service.settings.max_file_bytes, MAX_KNOWLEDGE_FILE_BYTES)
        with self.assertRaises(ValueError):
            KnowledgeSettings("http://127.0.0.1:3001", "key", "g" * 40,
                              max_file_bytes=MAX_KNOWLEDGE_FILE_BYTES + 1)

    def test_grants_expire_after_bounded_search_calls(self) -> None:
        token = self.service.issue_grant(self.event("grant-source"), ttl_seconds=60)
        grant_hash = self.service._grant_hash(token)
        with self.store.transaction() as connection:
            connection.execute("UPDATE knowledge_grants SET expires_at=? WHERE grant_hash=?",
                               ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), grant_hash))
        with self.assertRaises(KnowledgeAccessError):
            self.service.search_with_grant(token, "anything")
        with self.assertRaises(ValueError):
            self.service.issue_grant(self.event("bad-ttl"), ttl_seconds=60_000)

    def test_concurrent_searches_cannot_exceed_grant_call_limit(self) -> None:
        token = self.service.issue_grant(self.event("concurrent-grant"), ttl_seconds=60)

        def search(_index: int) -> bool:
            try:
                self.service.search_with_grant(token, "anything")
                return True
            except KnowledgeAccessError:
                return False

        with ThreadPoolExecutor(max_workers=32) as pool:
            successes = list(pool.map(search, range(40)))

        self.assertEqual(sum(successes), 20)
        grant_hash = self.service._grant_hash(token)
        row = self.store.connection.execute("SELECT call_count FROM knowledge_grants WHERE grant_hash=?",
                                            (grant_hash,)).fetchone()
        self.assertEqual(row["call_count"], 20)

    def test_selected_original_is_hash_checked_and_removed_after_delivery(self) -> None:
        event = self.event("delivery")
        document = self.ingest(event, "manual.md", "approved original")
        manager = WorkspaceManager(self.root / "workspaces")
        manager.provision("test-workspace")
        workspace = manager.get("test-workspace")
        delivery = KnowledgeDelivery(self.service, manager)
        staged = delivery.stage(event, workspace, "exec-1", [document["document_id"]])
        self.assertTrue(staged[0].relative_path.startswith("work/inputs/exec-1/"))
        staged_path = manager.validate_path(workspace.workspace_id, workspace.root / staged[0].relative_path)
        self.assertEqual(staged_path.read_text(encoding="utf-8"), "approved original")
        delivery.cleanup(workspace, "exec-1")
        self.assertFalse(staged_path.exists())

    def test_three_tier_rbac_downward_compat_toggle_and_location_ceiling(self) -> None:
        from app.identity import IdentityRegistry
        identities = IdentityRegistry(self.store)
        emp = identities.create_user("Employee Bob", role="employee")
        # Create two groups: one internal, one client_group; plus a customer DM
        identities.update_group(source_type="qq.group", source_id="internal-1", tier="internal")
        identities.update_group(source_type="qq.group", source_id="client-1", tier="client_group")

        # Employee saves internal note, client uploads client doc, customer uploads DM doc
        internal_doc = self.service.ingest_internal_text(
            actor_id=emp.user_id, title="pricing", text="company internal pricing table",
        )
        client_doc = self.ingest(self.event("c1-up", source_type="qq.group", source_id="client-1", actor_id="cust-1"),
                                 "client.txt", "client group contract")
        dm_doc = self.ingest(self.event("dm-up", source_type="qq.c2c", source_id="cust-dm", actor_id="cust-dm"),
                             "dm.txt", "customer private note")

        # 1. Compat ON (default): internal group & employee DM read internal + downward L1 client/DM
        emp_dm_grant = self.service.issue_grant(self.event("emp-q", "qq.c2c", "openid-bob", emp.user_id))
        internal_grp_grant = self.service.issue_grant(self.event("int-q", "qq.group", "internal-1", emp.user_id))
        self.assertEqual(
            {r["document_id"] for r in self.service.search_with_grant(emp_dm_grant, "evidence")["results"]},
            {internal_doc["document_id"], client_doc["document_id"], dm_doc["document_id"]},
        )
        self.assertEqual(
            {r["document_id"] for r in self.service.search_with_grant(internal_grp_grant, "evidence")["results"]},
            {internal_doc["document_id"], client_doc["document_id"], dm_doc["document_id"]},
        )

        # 2. Location Ceiling: even admin/employee inside L1 client-1 CANNOT see internal or DM docs!
        admin_in_client = self.event("adm-c1", source_type="qq.group", source_id="client-1", actor_id="administrator")
        ceil_grant = self.service.issue_grant(admin_in_client)
        self.assertEqual(
            {r["document_id"] for r in self.service.search_with_grant(ceil_grant, "evidence")["results"]},
            {client_doc["document_id"]},
        )
        with self.assertRaises(KnowledgeAccessError):
            self.service.authorize_documents(admin_in_client, [internal_doc["document_id"]])

        # 3. Turn Downward Compat OFF globally: L2 loses automatic L1 access!
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO settings(setting_key,payload,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(setting_key) DO UPDATE SET payload=excluded.payload",
                ("downward_compatibility_enabled", json.dumps({"value": False}), "2026-11-19T00:00:00Z"),
            )
        self.assertEqual(
            {r["document_id"] for r in self.service.search_with_grant(emp_dm_grant, "evidence")["results"]},
            {internal_doc["document_id"]},
        )

        # 4. Explicit Share bridges a specific client doc back to internal (and/or internal doc to a client group!)
        self.service.share_item(
            target_kind="document", target_id=client_doc["document_id"],
            target_scope_type="internal", target_scope_id="company", created_by="administrator",
        )
        self.assertEqual(
            {r["document_id"] for r in self.service.search_with_grant(emp_dm_grant, "evidence")["results"]},
            {internal_doc["document_id"], client_doc["document_id"]},
        )
        # Per-group override forces downward compat ON for internal-1 even while global is OFF
        identities.update_group(source_type="qq.group", source_id="internal-1", downward_compat="on")
        self.assertEqual(
            {r["document_id"] for r in self.service.search_with_grant(internal_grp_grant, "evidence")["results"]},
            {internal_doc["document_id"], client_doc["document_id"], dm_doc["document_id"]},
        )

        # 5. Admin L3 in DM retains full visibility across all documents regardless of compat switch
        admin_dm = self.event("adm-dm", source_type="qq.c2c", source_id="openid-admin", actor_id="administrator")
        admin_grant = self.service.issue_grant(admin_dm)
        self.assertEqual(
            {r["document_id"] for r in self.service.search_with_grant(admin_grant, "evidence")["results"]},
            {internal_doc["document_id"], client_doc["document_id"], dm_doc["document_id"]},
        )

        # 6. Sensitive .env attachment metadata stays L3-admin only even with downward compat ON
        env_event = self.event("env-att-up", source_type="qq.group", source_id="client-1", actor_id="cust-1")
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO attachments(attachment_id,event_id,payload,created_at) VALUES (?,?,?,?)",
                ("att-secret-1", env_event.event_id, json.dumps({
                    "attachment_id": "att-secret-1", "event_id": env_event.event_id,
                    "filename": ".env", "scope_type": "group", "scope_id": "client-1",
                    "sensitive": True, "status": "stored",
                }), "2026-11-19T00:00:00Z"),
            )
        with self.assertRaises(KnowledgeAccessError):
            self.service.read_attachment_with_grant(emp_dm_grant, "att-secret-1")
        listed_att_ids = {a["attachment_id"] for a in self.service.list_attachments_with_grant(emp_dm_grant)["attachments"]}
        self.assertNotIn("att-secret-1", listed_att_ids)


class KnowledgeApiTests(unittest.TestCase):
    def test_query_api_requires_a_valid_scoped_grant_and_does_not_return_raw_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "control.sqlite")
            client = FakeAnythingLLM()
            service = KnowledgeService(store, KnowledgeSettings("http://127.0.0.1", "key", "s" * 40),
                                       root / "knowledge-originals", client)
            event = KnowledgeServiceTests.event("api-source")
            path = root / "api.txt"
            path.write_text("searchable evidence", encoding="utf-8")
            service.ingest_file(event, 1, path, "api.txt")
            token = service.issue_grant(event)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_port}/internal/knowledge/search"
            try:
                for grant in ("invalid", token):
                    request = Request(endpoint, data=json.dumps({"grant": grant, "query": "evidence"}).encode(),
                                      headers={"Content-Type": "application/json"})
                    if grant == "invalid":
                        with self.assertRaises(HTTPError) as error:
                            urlopen(request)
                        self.assertEqual(error.exception.code, 403)
                        error.exception.close()
                    else:
                        response = json.loads(urlopen(request).read())
                        self.assertEqual(response["result_count"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.close()

    def test_query_server_binds_only_to_loopback_and_reports_dynamic_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "control.sqlite")
            service = KnowledgeService(store, KnowledgeSettings("http://127.0.0.1", "key", "s" * 40),
                                       Path(directory) / "knowledge-originals", FakeAnythingLLM())
            server = KnowledgeQueryServer(service, 0)
            try:
                self.assertEqual(server.address[0], "127.0.0.1")
                self.assertGreater(server.address[1], 0)
            finally:
                server.server.server_close()
                store.close()

    def test_non_loopback_query_binding_requires_a_bridge_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "API_KEY is required"):
            KnowledgeSettings("http://127.0.0.1", "key", "s" * 40,
                              query_api_host="192.0.2.10")

    def test_bridge_key_is_checked_before_spending_a_knowledge_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "control.sqlite")
            service = KnowledgeService(store, KnowledgeSettings("http://127.0.0.1", "key", "s" * 40),
                                       Path(directory) / "knowledge-originals", FakeAnythingLLM())
            token = service.issue_grant(KnowledgeServiceTests.event("bridge-auth"))
            bridge_key = "b" * 40
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, bridge_key))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_port}/internal/knowledge/search"
            body = json.dumps({"grant": token, "query": "evidence"}).encode()
            try:
                request = Request(endpoint, data=body, headers={"Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=2)
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
                self.assertEqual(store.connection.execute(
                    "SELECT call_count FROM knowledge_grants").fetchone()[0], 0)

                request = Request(endpoint, data=body, headers={"Content-Type": "application/json",
                               "X-Company-Agent-Bridge-Key": bridge_key})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.close()


if __name__ == "__main__":
    unittest.main()
