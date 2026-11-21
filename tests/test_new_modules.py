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

    def test_roles_groups_and_one_time_bind_codes(self):
        admin = self.users.create_user("Boss", role="admin")
        emp = self.users.create_user("Worker", role="employee")
        cust = self.users.create_user("Client")
        self.assertEqual(admin.role, "admin")
        self.assertEqual(emp.role, "employee")
        self.assertEqual(cust.role, "customer")
        self.assertEqual(self.users.set_user_role(cust.user_id, "employee").role, "employee")

        issued = self.users.create_bind_code(emp.user_id)
        redeemed = self.users.redeem_bind_code(issued["code"], provider="qq", account_id="app", external_id="ext-1")
        self.assertEqual(redeemed["user_id"], emp.user_id)
        self.assertEqual(self.users.resolve(provider="qq", account_id="app", scope_id="g1", external_id="ext-1"),
                         emp.user_id)
        with self.assertRaisesRegex(ValueError, "already been used"):
            self.users.redeem_bind_code(issued["code"], provider="qq", account_id="app", external_id="ext-2")

        g = self.users.register_group_seen("qq.group", "g-1")
        self.assertEqual(g.tier, "client_group")
        updated = self.users.update_group(source_id="g-1", tier="internal", downward_compat="off", display_name="Dev")
        self.assertEqual(updated.tier, "internal")
        self.assertEqual(updated.downward_compat, "off")
        self.assertEqual(updated.display_name, "Dev")

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

    def test_message_trace_keeps_running_status_when_only_ack_is_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            plane = ControlPlane.open(Path(directory) / "control.sqlite")
            try:
                event = self._seed_trace(plane)
                plane.create_work_item(work_item_id="work-ack-1", title="长任务", objective="调研",
                                       source_event_ids=[event.event_id], completion_conditions=["完成"],
                                       risk_class="low", owner_ref=event.actor_id)
                plane.propose_work_item("work-ack-1")
                plane.accept_work_item("work-ack-1")
                plane.queue_work_item("work-ack-1")
                plane.create_plan(plan_id="plan-ack-1", work_item_id="work-ack-1",
                                  steps=[PlanStep("step-ack-1", 1, "执行", capability="dsh.task")])
                plane.create_execution_request(execution_id="execution-ack-1", work_item_id="work-ack-1",
                                               plan_step_id="step-ack-1", workspace_id="workspace-ack-1",
                                               capability="dsh.task", parameters={"prompt": "process"})
                plane.save_assistant_run(AssistantRun(event_id=event.event_id, status="executing",
                                                      policy_version="test-policy", conversation_id=event.conversation_id,
                                                      thread_id="thread-1", work_item_id="work-ack-1",
                                                      plan_id="plan-ack-1", execution_ids=["execution-ack-1"],
                                                      workspace_id="workspace-ack-1"))
                plane.start_execution("execution-ack-1")
                ack_decision = plane.decide_publication(
                    decision_id="ack-decision-1", work_item_id="work-ack-1",
                    conversation_id=event.conversation_id, disposition="group_update",
                    reason="ack", policy_version="test-policy", body="已收到任务",
                    idempotency_key=f"qq-ack:{event.event_id}",
                    destination={"channel": "qq", "group_openid": "group-1"},
                )
                plane.mark_outbox_attempt(ack_decision.outbox_id)
                plane.mark_outbox_sent(ack_decision.outbox_id)
                trace = trace_for_event(plane.store, event.event_id)
                self.assertEqual(trace["status"], "dsh_running")
                self.assertEqual(trace["status_label"], "DSH 正在执行")
                self.assertEqual(trace["outbox"][0]["kind"], "ack")
            finally:
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

    def test_admin_api_manages_roles_groups_bind_codes_compat_shares_and_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            plane = ControlPlane.open(Path(directory) / "control.sqlite")
            from app.knowledge.config import KnowledgeSettings
            from app.knowledge.service import KnowledgeService

            class FakeLLM:
                def ensure_workspace(self, _slug): return "w"
                def upload_document(self, _path, _name, **_kwargs): return "doc-loc-1"
                def upload_text_file(self, _w, _p, *, source_name): return "d"
                def upload_binary_file(self, _w, _p, *, source_name, content_type): return "d"
                def search(self, _w, _q, *, limit=8): return []

            ks = KnowledgeService(plane.store, KnowledgeSettings("http://127.0.0.1", "k", "s" * 40),
                                  Path(directory) / "knowledge", FakeLLM())
            note_doc = ks.ingest_internal_text(actor_id="emp-1", title="sop", text="internal handbook")
            server = HTTPServer(("127.0.0.1", 0), make_handler(plane, Path(directory), "admin-tok", knowledge_service=ks))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def call(path, method="GET", body=None):
                req = Request(
                    base + path,
                    data=json.dumps(body).encode() if body is not None else None,
                    method=method,
                    headers={"Authorization": "Bearer admin-tok", "Content-Type": "application/json"},
                )
                with urlopen(req, timeout=2) as resp:
                    return json.loads(resp.read())

            try:
                user = call("/api/users", "POST", {"display_name": "Alice", "role": "employee"})
                self.assertEqual(user["role"], "employee")
                promoted = call("/api/users/role", "PUT", {"user_id": user["user_id"], "role": "admin"})
                self.assertEqual(promoted["role"], "admin")
                code_resp = call("/api/bind-codes", "POST", {"user_id": user["user_id"]})
                self.assertIn("-", code_resp["code"])

                grp = call("/api/groups", "PUT", {
                    "source_id": "g-vip", "tier": "internal", "downward_compat": "off", "display_name": "VIP群",
                })
                self.assertEqual(grp["group"]["tier"], "internal")
                self.assertEqual(call("/api/groups")["groups"][0]["display_name"], "VIP群")

                self.assertTrue(call("/api/settings/downward-compat")["enabled"])
                self.assertFalse(call("/api/settings/downward-compat", "PUT", {"enabled": False})["enabled"])
                self.assertFalse(call("/api/status")["downward_compatibility_enabled"])

                shared = call("/api/knowledge/shares", "POST", {
                    "target_kind": "document", "target_id": note_doc["document_id"],
                    "target_scope_type": "group", "target_scope_id": "client-g1", "note": "manual share",
                })["share"]
                self.assertEqual(len(call("/api/knowledge/shares")["shares"]), 1)
                self.assertTrue(call("/api/knowledge/shares?share_id=" + shared["share_id"], "DELETE")["removed"])

                saved_tool = call("/api/tools/scripts", "POST", {
                    "layer": "common", "name": "ping.sh", "content": "#!/bin/sh\necho pong\n", "description": "ping",
                })["script"]
                self.assertEqual(saved_tool["name"], "ping.sh")
                self.assertEqual(call("/api/tools/script?layer=common&name=ping.sh")["name"], "ping.sh")
                self.assertTrue(call("/api/tools/scripts?layer=common&name=ping.sh", "DELETE")["removed"])

                status_payload = call("/api/status")
                self.assertIn("recent_dsh", status_payload)
                self.assertIn("knowledge_summary", status_payload)
                users_payload = call("/api/users")
                self.assertIn("seen_external_actors", users_payload)
                knowledge_payload = call("/api/knowledge")
                self.assertIn("attachments", knowledge_payload)
                logs_payload = call("/api/logs?entity_type=chat_group")
                self.assertTrue(all(item["entity_type"] == "chat_group" for item in logs_payload["logs"]))
                self.assertTrue(logs_payload["logs"][0]["label"])

                from app.workspace import WorkspaceManager
                ws_mgr = WorkspaceManager(Path(directory) / "workspaces")
                ws_mgr.provision("ws-dsh-test", tier="internal")
                ws_mgr.append_log(
                    "ws-dsh-test",
                    "dsh/exec_dsh_1.events.jsonl",
                    json.dumps({"type": "tool_call", "tool": "bash", "input": {"command": "echo hi"}}) + "\n",
                )
                ws_mgr.save_execution_record("ws-dsh-test", {
                    "execution_id": "exec_dsh_1",
                    "runtime": "dsh",
                    "status": "succeeded",
                    "result": {
                        "execution_id": "exec_dsh_1",
                        "status": "succeeded",
                        "output": "done ok",
                        "log_paths": ["logs/dsh/exec_dsh_1.events.jsonl"],
                        "artifact_paths": ["artifacts/step-01.md"],
                        "metadata": {"tools_used": ["bash"]},
                    },
                })
                dsh_logs = call("/api/workspace-logs?workspace_id=ws-dsh-test&execution_id=exec_dsh_1")
                self.assertIn("[tool_call] bash", dsh_logs["stdout"])
                self.assertIn("done ok", dsh_logs["stdout"])
                self.assertEqual(dsh_logs["stderr"], "")

                # Capability gaps API + bot behavior settings API
                gap = plane.record_capability_gap(
                    event_id="ev-gap-api-1",
                    source_type="qq.group",
                    source_id="g-vip",
                    conversation_id="g-vip",
                    group_tier="internal",
                    topic="统计本月套餐利润率",
                    summary="内部群聊到利润率遇冷",
                    missing_capability="ERP 成本接入脚本",
                    gap_kind="tool_script",
                    suggested_solution="加入 calc_margin.py 后可自动产出报告",
                )
                gaps_resp = call("/api/capability-gaps")
                self.assertEqual(gaps_resp["open_count"], 1)
                self.assertEqual(gaps_resp["gaps"][0]["group_display_name"], "VIP群")
                resolved_resp = call("/api/capability-gaps", "PUT", {
                    "gap_id": gap["gap_id"], "status": "resolved", "resolution_note": "已添加 calc_margin.py",
                })
                self.assertEqual(resolved_resp["gap"]["status"], "resolved")
                self.assertEqual(call("/api/capability-gaps?status=open")["open_count"], 0)

                bot_cfg = call("/api/settings/bot-behavior", "PUT", {
                    "ambient_listen_enabled": True,
                    "ambient_cooldown_seconds": 3600,
                    "bot_aliases": "憨憨, 智能助理",
                })
                self.assertTrue(bot_cfg["ambient_listen_enabled"])
                self.assertEqual(bot_cfg["ambient_cooldown_seconds"], 3600.0)
                self.assertEqual(bot_cfg["bot_aliases"], ["憨憨", "智能助理"])
                self.assertEqual(call("/api/status")["bot_behavior"]["bot_aliases"], ["憨憨", "智能助理"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                plane.close()

    def test_observability_conversations_sort_mixed_timezones_and_enrich_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            plane = ControlPlane.open(Path(directory) / "control.sqlite")
            try:
                identities = IdentityRegistry(plane.store)
                emp = identities.create_user("Zhang Employee", role="employee")
                identities.bind(
                    provider="qq", account_id="bot1", scope_id="grp-1",
                    external_id="qq-emp-1", user_id=emp.user_id,
                )
                identities.update_group(
                    source_type="qq.group", source_id="grp-1",
                    tier="internal", downward_compat="off", display_name="研发内部群",
                )
                e1 = Event(
                    event_id="qq:bot1:grp-1:m1",
                    source_type="qq.group",
                    source_id="grp-1",
                    conversation_id="qq.group:grp-1",
                    actor_id=emp.user_id,
                    external_actor_id="qq-emp-1",
                    content="早上的需求 (+08:00 10:00 = UTC 02:00)",
                    occurred_at="2026-11-19T10:00:00+08:00",
                    attachments=[],
                    capabilities=["reply"],
                )
                e2 = Event(
                    event_id="qq:bot1:grp-1:m2",
                    source_type="qq.group",
                    source_id="grp-1",
                    conversation_id="qq.group:grp-1",
                    actor_id=emp.user_id,
                    external_actor_id="qq-emp-1",
                    content="上午的需求 (+00:00 03:00 = +08:00 11:00)",
                    occurred_at="2026-11-19T03:00:00+00:00",
                    attachments=[],
                    capabilities=["reply"],
                )
                plane.ingest_event(e1)
                plane.ingest_event(e2)
                plane.save_assistant_run(AssistantRun(
                    event_id=e2.event_id,
                    status="completed",
                    policy_version="v1",
                    workspace_id="qq-group-grp-1",
                ))

                tr = trace_for_event(plane.store, e2.event_id)
                self.assertIsNotNone(tr)
                self.assertEqual(tr["actor_display_name"], "Zhang Employee")
                self.assertEqual(tr["actor_role"], "employee")
                self.assertEqual(tr["group_display_name"], "研发内部群")
                self.assertEqual(tr["group_tier"], "internal")
                self.assertEqual(tr["downward_compat"], "off")
                self.assertEqual(tr["workspace_id"], "qq-group-grp-1")

                convs = list_conversations(plane.store, limit=10)
                self.assertEqual(len(convs), 1)
                self.assertEqual(convs[0]["label"], "研发内部群")
                self.assertEqual(convs[0]["group_tier"], "internal")
                self.assertEqual(convs[0]["workspace_id"], "qq-group-grp-1")
                # Crucial: 03:00+00:00 (UTC 03:00) is LATER than 10:00+08:00 (UTC 02:00) even though lexicographically "03:" < "10:"
                self.assertEqual([m["event_id"] for m in convs[0]["messages"]], [e1.event_id, e2.event_id])
            finally:
                plane.close()


if __name__ == "__main__":
    unittest.main()
