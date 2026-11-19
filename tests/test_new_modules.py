from __future__ import annotations

import json
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from http.server import HTTPServer, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.admin_api import make_handler
from app.attachments import AttachmentIngestor, _resolve_attachment_addresses, extract_text
from app.knowledge.file_types import detect_file
from app.control_plane import ControlPlane
from app.identity import IdentityRegistry
from app.models import AssistantRun, Event, PlanStep
from app.observability import list_conversations, trace_for_event
from app.runtime_config import RuntimeConfig
from app.scheduler import ScheduleService, ScheduledTask, SchedulerWorker
from app.scheduled_resume import ScheduledResumeHandler
from app.qq_channel import QQChannelService
from app.runtime_config import QQBotSettings
from app.store import Store
from app.web_gate import make_handler as make_web_gate_handler


class IdentityRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "control.sqlite")
        self.users = IdentityRegistry(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_platform_user_can_be_bound_and_group_scope_falls_back_to_global(self):
        user = self.users.create_user("Alice", time_zone="Asia/Shanghai")
        binding = self.users.bind(provider="qq", account_id="app", scope_id="*",
                                  external_id="member", user_id=user.user_id)
        self.assertEqual(self.users.resolve(provider="qq", account_id="app", scope_id="group-1",
                                            external_id="member"), user.user_id)
        self.assertTrue(self.users.unbind(binding.binding_id))
        self.assertTrue(self.users.resolve(provider="qq", account_id="app", scope_id="group-1",
                                           external_id="member").startswith("external:qq:app:group-1:"))

    def test_invalid_timezone_and_binding_to_missing_user_are_rejected(self):
        with self.assertRaises(ValueError):
            self.users.create_user("Alice", time_zone="Mars/Olympus")
        with self.assertRaises(ValueError):
            self.users.bind(provider="qq", account_id="app", scope_id="g", external_id="m", user_id="missing")


class ScheduleServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "control.sqlite")
        self.schedules = ScheduleService(self.store)
        now = "2026-11-16T00:00:00+00:00"
        with self.store.transaction() as connection:
            for event_id in ("event-1", "e"):
                connection.execute("INSERT INTO events(event_id,payload,created_at) VALUES (?,?,?)",
                                   (event_id, json.dumps({"event_id": event_id}), now))
            for work_id in ("work-1", "w"):
                connection.execute("INSERT INTO work_items(work_item_id,payload,status,created_at,updated_at) "
                                   "VALUES (?,?,?,?,?)", (work_id, "{}", "proposed", now, now))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_local_time_is_normalized_to_utc_and_context_survives_wakeup(self):
        task = self.schedules.create(due_at="2026-11-17T09:00:00", time_zone="Asia/Shanghai",
                                     source_event_id="event-1", work_item_id="work-1",
                                     resume_prompt="Continue", context={"summary": "Saved context"})
        self.assertEqual(task.due_at, "2026-11-17T01:00:00+00:00")
        self.assertEqual(task.payload["context"]["summary"], "Saved context")
        due = self.schedules.claim_due(now=datetime(2026, 11, 17, 2, tzinfo=timezone.utc))
        self.assertEqual(due[0].schedule_id, task.schedule_id)
        self.assertEqual(self.schedules.finish(task.schedule_id).status, "completed")

    def test_daylight_saving_ambiguous_local_time_requires_offset(self):
        with self.assertRaises(ValueError):
            self.schedules.create(due_at="2026-11-01T01:30:00", time_zone="America/New_York",
                                  source_event_id="e", work_item_id="w", resume_prompt="continue")

    def test_interrupted_tasks_are_requeued_and_failed_tasks_can_retry(self):
        task = self.schedules.create(due_at="2000-01-01T00:00:00Z", time_zone="UTC",
                                     source_event_id="e", work_item_id="w", resume_prompt="continue")
        self.schedules.claim_due(now=datetime(2026, 11, 17, tzinfo=timezone.utc))
        self.assertEqual(self.schedules.recover_interrupted(), 1)
        claimed = self.schedules.claim_due(now=datetime(2026, 11, 17, tzinfo=timezone.utc))[0]
        self.schedules.finish(claimed.schedule_id, error="temporary failure")
        self.assertEqual(self.schedules.retry(task.schedule_id).status, "scheduled")

    def test_worker_invokes_due_resume_callback_and_completes_task(self):
        task = self.schedules.create(due_at="2000-01-01T00:00:00Z", time_zone="UTC",
                                     source_event_id="e", work_item_id="w", resume_prompt="continue")
        seen = []
        worker = SchedulerWorker(self.schedules, lambda item: seen.append(item.schedule_id))
        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(seen, [task.schedule_id])
        self.assertEqual(self.schedules.list()[0].status, "completed")


class ScheduledResumeTests(unittest.TestCase):
    def test_wakeup_reuses_source_conversation_and_saved_context(self):
        with tempfile.TemporaryDirectory() as directory:
            plane = ControlPlane.open(Path(directory) / "control.sqlite")
            source = Event("event-1", "qq.group", "group", "group", "user-1",
                           "2026-11-16T00:00:00Z", "Initial issue context", [], ["dsh.task"])
            plane.ingest_event(source)

            class Workflow:
                seen = None
                def process_event(self, event):
                    self.seen = event
                    return type("Run", (), {"to_dict": lambda self: {}})()

            class Channel:
                def __init__(self):
                    self.plane, self.workflow = plane, Workflow()

            channel = Channel()
            task = ScheduledTask("schedule-1", "running", "2026-11-17T00:00:00Z",
                {"source_event_id": source.event_id, "work_item_id": "work-1", "resume_prompt": "Continue analysis",
                 "context": {"source_event": source.to_dict(), "summary": "Important decision"}, "destination": {}}, "", "")
            ScheduledResumeHandler(channel)(task)
            self.assertEqual(channel.workflow.seen.conversation_id, source.conversation_id)
            self.assertIn("Important decision", channel.workflow.seen.content)
            self.assertEqual(channel.workflow.seen.event_id, "schedule-wake:schedule-1")
            plane.close()


class AttachmentTests(unittest.TestCase):
    def test_qq_attachment_fake_dns_uses_validated_public_dns_addresses(self):
        fake_address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.165", 443))
        with patch("app.attachments.socket.getaddrinfo", return_value=[fake_address]), \
                patch("app.attachments._resolve_qq_attachment_addresses",
                      return_value=("183.60.225.96",)) as resolve_qq:
            addresses = _resolve_attachment_addresses("gzc-download.ftn.qq.com", 5)

        self.assertEqual(addresses, ("183.60.225.96",))
        resolve_qq.assert_called_once_with("gzc-download.ftn.qq.com", 5)

    def test_non_qq_attachment_hosts_still_reject_private_dns_answers(self):
        fake_address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.165", 443))
        with patch("app.attachments.socket.getaddrinfo", return_value=[fake_address]):
            with self.assertRaisesRegex(ValueError, "private or reserved"):
                _resolve_attachment_addresses("files.example", 5)

    def test_csv_and_text_extraction_are_bounded_and_structured(self):
        self.assertEqual(extract_text(b"name,count\nA,3\n", "stats.csv", "text/csv"), "name\tcount\nA\t3")
        self.assertEqual(extract_text(b"hello\nworld", "note.txt", "text/plain", max_chars=5), "hello")

    def test_image_signatures_are_detected_and_local_parser_defers_to_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            png = Path(directory) / "image.bin"
            png.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            detected = detect_file(png, "中文图片.dat")
            self.assertEqual(detected.filename, "中文图片.png")
            self.assertEqual(detected.content_type, "image/png")
            jpeg = Path(directory) / "photo.bin"
            jpeg.write_bytes(b"\xff\xd8\xff\xe0JPEG payload")
            detected_jpeg = detect_file(jpeg, "photo.jpeg")
            self.assertEqual(detected_jpeg.filename, "photo.jpg")
            self.assertEqual(detected_jpeg.content_type, "image/jpeg")
        with self.assertRaisesRegex(ValueError, "AnythingLLM OCR"):
            extract_text(b"\xff\xd8\xffimage", "photo.jpg", "image/jpeg")

    def test_attachment_event_persists_placeholder_then_original_without_mutating_message(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "control.sqlite")
            ingestor = AttachmentIngestor(store, storage_directory=Path(directory) / "attachments")
            self.assertTrue(callable(getattr(AttachmentIngestor, "_download_to", None)))
            ingestor._download_to = lambda url, destination: (destination.write_bytes(b"Report result: 42") or 17)  # type: ignore[method-assign]
            from app.models import Event
            event = Event("event-1", "qq.group", "g", "g", "u", "2026-11-16T00:00:00Z", "Please read",
                          [{"filename": "report.txt", "content_type": "text/plain", "url": "https://files.example/a"}],
                          ["dsh.task"])
            registered = ingestor.register_event(event)
            self.assertEqual(registered.content, "Please read")
            self.assertEqual(registered.attachments[0]["status"], "received")
            enriched = ingestor.process_event(registered)
            self.assertEqual(enriched.content, "Please read")
            row = store.connection.execute("SELECT payload FROM attachments").fetchone()
            payload = json.loads(row[0])
            self.assertEqual(payload["status"], "stored")
            self.assertTrue(Path(payload["original_path"]).is_file())
            store.close()

    def test_qq_message_uses_bound_internal_identity_and_passes_file_text_to_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            plane = ControlPlane.open(Path(directory) / "control.sqlite")
            identities = IdentityRegistry(plane.store)
            user = identities.create_user("Bound user")
            identities.bind(provider="qq", account_id="app", scope_id="group", external_id="member",
                            user_id=user.user_id)

            class Workflow:
                received = None
                def process_event(self, event):
                    self.received = event
                    plane.ingest_event(event)
                    return type("Run", (), {"to_dict": lambda self: {}})()

            class Api:
                def send_group_message(self, _message):
                    raise AssertionError("no outbound message expected")

            service = QQChannelService(plane, Workflow(), Api(), QQBotSettings("app", "secret"),
                                       identities=identities,
                                       attachment_ingestor=AttachmentIngestor(
                                           plane.store, storage_directory=Path(directory) / "attachments"))
            service.attachment_ingestor._download_to = lambda url, destination: (destination.write_bytes(b"Attachment body") or 15)  # type: ignore[method-assign]
            service.handle_gateway_event("GROUP_MESSAGE_CREATE", {
                "id": "message-1", "group_openid": "group", "author": {"member_openid": "member"},
                "content": "Please inspect", "timestamp": "2026-11-16T00:00:00Z",
                "attachments": [{"filename": "note.txt", "content_type": "text/plain", "url": "https://cdn.example/note"}],
            })
            saved = plane.get_event("qq:app:group:message-1")
            self.assertEqual(saved.actor_id, user.user_id)
            self.assertEqual(saved.external_actor_id, "member")
            self.assertEqual(saved.content, "Please inspect")
            self.assertEqual(saved.attachments[0]["status"], "stored")
            payload = plane.store.decode(plane.store.connection.execute("SELECT payload FROM attachments").fetchone()["payload"])
            self.assertTrue(Path(payload["original_path"]).is_file())
            plane.close()


class WebSearchConfigTests(unittest.TestCase):
    def test_web_patch_respects_explicit_setting(self):
        config = RuntimeConfig("https://model.example/v1", "model", "secret")
        patch = {item["id"]: item for item in config.provider_patch(web_enabled=True)}
        self.assertFalse(patch["tool-web"]["disabled"])
        self.assertTrue({item["id"]: item for item in config.provider_patch(web_enabled=False)}["tool-web"]["disabled"])


class AdminApiTests(unittest.TestCase):
    def _seed_trace(self, plane):
        event = Event("event-trace-1", "qq.group", "group-1", "conversation-1", "user-1",
                      "2026-11-18T10:00:00+00:00", "请处理这个文件", [{"filename": "report.md", "status": "stored"}],
                      ["dsh.task"], external_actor_id="qq-user-1")
        plane.create_conversation(conversation_id=event.conversation_id, kind=event.source_type,
                                  participant_refs=[event.actor_id], policy_version="test-policy")
        plane.ingest_event(event)
        plane.save_assistant_run(AssistantRun(event_id=event.event_id, status="awaiting_decision",
                                              policy_version="test-policy", conversation_id=event.conversation_id,
                                              thread_id="thread-1"))
        return event

    def test_conversation_view_and_message_trace_expose_pipeline_without_raw_log_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            plane = ControlPlane.open(Path(directory) / "control.sqlite")
            event = self._seed_trace(plane)
            conversations = list_conversations(plane.store)
            self.assertEqual(conversations[0]["conversation_id"], event.conversation_id)
            self.assertEqual(conversations[0]["messages"][0]["content"], event.content)
            trace = trace_for_event(plane.store, event.event_id)
            self.assertEqual(trace["status_label"], "等待 DSSuperAgent 判断")
            self.assertEqual(trace["timeline"][0]["label"], "Company Agent 已收到消息")
            self.assertNotIn("decision", trace["assistant_run"] or {})
            plane.close()

    def test_message_trace_links_work_execution_and_reply_states(self):
        with tempfile.TemporaryDirectory() as directory:
            plane = ControlPlane.open(Path(directory) / "control.sqlite")
            event = self._seed_trace(plane)
            plane.create_work_item(work_item_id="work-trace-1", title="处理文件", objective="生成结论",
                                   source_event_ids=[event.event_id], completion_conditions=["结果已生成"],
                                   risk_class="low", owner_ref=event.actor_id)
            plane.propose_work_item("work-trace-1")
            plane.accept_work_item("work-trace-1")
            plane.queue_work_item("work-trace-1")
            plane.create_plan(plan_id="plan-trace-1", work_item_id="work-trace-1",
                              steps=[PlanStep("step-trace-1", 1, "处理", capability="dsh.task")])
            plane.create_execution_request(execution_id="execution-trace-1", work_item_id="work-trace-1",
                                           plan_step_id="step-trace-1", workspace_id="workspace-trace-1",
                                           capability="dsh.task", parameters={"prompt": "process"})
            plane.save_assistant_run(AssistantRun(event_id=event.event_id, status="executing",
                                                  policy_version="test-policy", conversation_id=event.conversation_id,
                                                  thread_id="thread-1", work_item_id="work-trace-1",
                                                  plan_id="plan-trace-1", execution_ids=["execution-trace-1"],
                                                  workspace_id="workspace-trace-1"))
            plane.decide_publication(decision_id="publication-trace-1", work_item_id="work-trace-1",
                                     conversation_id=event.conversation_id, disposition="group_update",
                                     reason="test", policy_version="test-policy", body="处理完成",
                                     idempotency_key="qq-publication:event-trace-1",
                                     destination={"channel": "qq", "group_openid": "group-1"})
            trace = trace_for_event(plane.store, event.event_id)
            self.assertEqual(trace["status_label"], "等待发送回复")
            self.assertEqual(trace["work_item"]["work_item_id"], "work-trace-1")
            self.assertEqual(trace["executions"][0]["status"], "queued")
            self.assertEqual(trace["outbox"][0]["status"], "pending")
            plane.close()

    def test_api_requires_bearer_token_and_returns_control_state(self):
        with tempfile.TemporaryDirectory() as directory:
            plane = ControlPlane.open(Path(directory) / "control.sqlite")
            event = self._seed_trace(plane)
            server = HTTPServer(("127.0.0.1", 0), make_handler(plane, Path(directory), "secret-token"))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/api/status"
            try:
                with self.assertRaises(HTTPError) as error:
                    urlopen(url)
                self.assertEqual(error.exception.code, 401)
                error.exception.close()
                request = Request(url, headers={"Authorization": "Bearer secret-token"})
                result = json.loads(urlopen(request).read())
                self.assertEqual(result["service"], "running")
                page = urlopen(f"http://127.0.0.1:{server.server_port}/").read().decode()
                self.assertIn("Company Agent", page)
                setting_request = Request(f"http://127.0.0.1:{server.server_port}/api/settings/web-search",
                    data=json.dumps({"enabled": True}).encode(), method="PUT",
                    headers={"Authorization": "Bearer secret-token", "Content-Type": "application/json"})
                self.assertTrue(json.loads(urlopen(setting_request).read())["enabled"])
                status_request = Request(url, headers={"Authorization": "Bearer secret-token"})
                self.assertTrue(json.loads(urlopen(status_request).read())["web_search_enabled"])
                dssuperagent_request = Request(f"http://127.0.0.1:{server.server_port}/api/settings/dssuperagent-web-search",
                    data=json.dumps({"enabled": True}).encode(), method="PUT",
                    headers={"Authorization": "Bearer secret-token", "Content-Type": "application/json"})
                self.assertTrue(json.loads(urlopen(dssuperagent_request).read())["enabled"])
                dssuperagent_state = json.loads(urlopen(status_request).read())
                self.assertTrue(dssuperagent_state["dssuperagent_web_search_enabled"])
                dsh_request = Request(f"http://127.0.0.1:{server.server_port}/api/settings/dsh-web-search",
                    data=json.dumps({"enabled": False}).encode(), method="PUT",
                    headers={"Authorization": "Bearer secret-token", "Content-Type": "application/json"})
                self.assertFalse(json.loads(urlopen(dsh_request).read())["enabled"])
                final_state = json.loads(urlopen(status_request).read())
                self.assertTrue(final_state["dssuperagent_web_search_enabled"])
                self.assertFalse(final_state["dsh_web_search_enabled"])
                conversation_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/conversations",
                    headers={"Authorization": "Bearer secret-token"})
                conversations = json.loads(urlopen(conversation_request).read())
                self.assertIn("conversations", conversations)
                trace_request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/message-trace?event_id={event.event_id}",
                    headers={"Authorization": "Bearer secret-token"})
                trace = json.loads(urlopen(trace_request).read())
                self.assertEqual(trace["event_id"], event.event_id)
                self.assertEqual(trace["status_label"], "等待 DSSuperAgent 判断")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                plane.close()


    def test_console_switch_controls_dssuperagent_worker_search_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            plane = ControlPlane.open(Path(directory) / "control.sqlite")
            admin_server = HTTPServer(("127.0.0.1", 0), make_handler(plane, Path(directory), "admin-token"))
            web_server = ThreadingHTTPServer(("127.0.0.1", 0), make_web_gate_handler(
                enabled=lambda: bool(plane.get_setting("dssuperagent_web_search_enabled", False)),
                api_key="bridge-secret",
                search=lambda query, count: {"query": query, "sources": [{"url": "https://example.com/", "title": "Example"}], "truncated": False},
            ))
            threads = [Thread(target=server.serve_forever, daemon=True) for server in (admin_server, web_server)]
            for thread in threads:
                thread.start()
            setting_url = f"http://127.0.0.1:{admin_server.server_port}/api/settings/dssuperagent-web-search"
            search_url = f"http://127.0.0.1:{web_server.server_port}/internal/web/search"

            def set_enabled(enabled):
                request = Request(setting_url, data=json.dumps({"enabled": enabled}).encode(), method="PUT",
                    headers={"Authorization": "Bearer admin-token", "Content-Type": "application/json"})
                with urlopen(request, timeout=2) as response:
                    return json.loads(response.read())

            def search():
                request = Request(search_url, data=json.dumps({"query": "latest release"}).encode(), method="POST",
                    headers={"Content-Type": "application/json", "X-Company-Agent-Bridge-Key": "bridge-secret"})
                with urlopen(request, timeout=2) as response:
                    return json.loads(response.read())

            try:
                with self.assertRaises(HTTPError) as error:
                    search()
                self.assertEqual(error.exception.code, 403)
                self.assertTrue(set_enabled(True)["enabled"])
                self.assertEqual(search()["sources"][0]["url"], "https://example.com/")
                self.assertFalse(set_enabled(False)["enabled"])
                with self.assertRaises(HTTPError) as error:
                    search()
                self.assertEqual(error.exception.code, 403)
            finally:
                for server in (admin_server, web_server):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=2)
                plane.close()


if __name__ == "__main__":
    unittest.main()
