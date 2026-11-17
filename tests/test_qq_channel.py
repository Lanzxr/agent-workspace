from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.control_plane import ControlPlane
from app.attachments import AttachmentIngestor
from app.knowledge.config import KnowledgeSettings
from app.knowledge.delivery import KnowledgeDelivery
from app.knowledge.service import KnowledgeService
from app.workspace import WorkspaceManager
from app.models import ExecutionResult
from app.assistant_workflow import AssistantPolicy, DssuperAgentWorkflow
from app.dssuperagent import DssuperAgentDecision, DssuperAgentReceipt, DssuperAgentTimeout
from app.models import Event
from app.qq_channel import QQBotAPI, QQChannelService, QQGateway, normalize_group_message
from app.models import OutboxMessage
from app.runtime_config import QQBotSettings
from app.workspace import WorkspaceManager


class QQMessageNormalizationTests(unittest.TestCase):
    def test_non_mention_group_message_becomes_a_stable_standard_event(self) -> None:
        payload = {
            "id": "message-123",
            "group_openid": "group-opaque",
            "author": {"member_openid": "member-opaque", "username": "Test User", "bot": False},
            "content": "Please check this",
            "timestamp": "2026-11-15T01:02:03+08:00",
            "attachments": [{"content_type": "image/png", "url": "https://example.test/a.png", "secret": "drop"}],
        }
        event, destination = normalize_group_message("app-1", "GROUP_MESSAGE_CREATE", payload)
        self.assertEqual(event.event_id, "qq:app-1:group:message-123")
        self.assertEqual(event.source_type, "qq.group")
        self.assertEqual(event.source_id, "group-opaque")
        self.assertEqual(event.conversation_id, "group-opaque")
        self.assertEqual(event.actor_id, "member-opaque")
        self.assertEqual(event.content, "Please check this")
        self.assertEqual(event.attachments, [{"content_type": "image/png", "url": "https://example.test/a.png"}])
        self.assertEqual(event.capabilities, ["dsh.task"])
        self.assertEqual(destination, {"channel": "qq", "group_openid": "group-opaque",
                                       "reply_to_message_id": "message-123",
                                       "reply_deadline_epoch": "1794676023.0"})

    def test_bot_messages_and_other_event_types_are_ignored(self) -> None:
        payload = {"id": "message-123", "group_openid": "group-opaque",
                   "author": {"member_openid": "bot-member", "bot": True},
                   "content": "echo", "timestamp": "2026-11-15T01:02:03Z"}
        self.assertIsNone(normalize_group_message("app-1", "GROUP_MESSAGE_CREATE", payload))
        self.assertIsNone(normalize_group_message("app-1", "GROUP_MEMBER_ADD", payload))

    def test_malformed_group_message_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_group_message("app-1", "GROUP_MESSAGE_CREATE", {"id": "message-123"})

    def test_attachment_only_message_gets_nonempty_event_content(self) -> None:
        payload = {"id": "m", "group_openid": "g", "author": {"member_openid": "u"},
                   "content": "", "timestamp": "2026-11-15T00:00:00Z",
                   "attachments": [{"content_type": "image/png"}]}
        event, _ = normalize_group_message("a", "GROUP_MESSAGE_CREATE", payload)
        self.assertEqual(event.content, "[附件]")


class QQConfigTests(unittest.TestCase):
    def test_existing_runtime_config_key_aliases_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text("QQBOT_APPID=app\nQQBOT_SECRET=secret\n", encoding="utf-8")

            settings = QQBotSettings.load(path)

            self.assertEqual(settings.app_id, "app")
            self.assertEqual(settings.client_secret, "secret")
            self.assertEqual(settings.allowed_publications, frozenset())
            self.assertEqual(settings.api_base_url, "https://api.bot.qq.com")

    def test_publication_allowlist_defaults_to_empty_and_accepts_supported_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text("QQBOT_APP_ID=app\nQQBOT_CLIENT_SECRET=secret\n", encoding="utf-8")
            self.assertEqual(QQBotSettings.load(path).allowed_publications, frozenset())
            path.write_text("QQBOT_APP_ID=app\nQQBOT_CLIENT_SECRET=secret\n"
                            "QQBOT_ALLOWED_PUBLICATIONS=group_update,urgent_escalation\n", encoding="utf-8")
            self.assertEqual(QQBotSettings.load(path).allowed_publications,
                             {"group_update", "urgent_escalation"})

    def test_unknown_publication_disposition_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text("QQBOT_APP_ID=app\nQQBOT_CLIENT_SECRET=secret\n"
                            "QQBOT_ALLOWED_PUBLICATIONS=private_nudge\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "supports group_update"):
                QQBotSettings.load(path)

    def test_dsh_auto_execution_requires_an_explicit_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text("QQBOT_APP_ID=app\nQQBOT_CLIENT_SECRET=secret\n"
                            "QQBOT_AUTO_EXECUTE_CAPABILITIES=dsh.task\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "COMPANY_AGENT_QQ_WORKSPACE is required"):
                QQBotSettings.load(path)

            path.write_text(path.read_text(encoding="utf-8") + "COMPANY_AGENT_QQ_WORKSPACE=qq-agent\n",
                            encoding="utf-8")
            settings = QQBotSettings.load(path)
            self.assertEqual(settings.auto_execute_capabilities, frozenset({"dsh.task"}))
            self.assertEqual(settings.workspace_id, "qq-agent")

    def test_unknown_auto_execution_capability_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text("QQBOT_APP_ID=app\nQQBOT_CLIENT_SECRET=secret\n"
                            "QQBOT_AUTO_EXECUTE_CAPABILITIES=admin.shell\n"
                            "COMPANY_AGENT_QQ_WORKSPACE=qq-agent\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "supports only dsh.task"):
                QQBotSettings.load(path)

class QQServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.plane = ControlPlane.open(Path(self.directory.name) / "control.sqlite")
        self.event = Event("qq:app:group:message", "qq.group", "group", "group", "member",
                           "2026-11-15T00:00:00Z", "Please review this", [], ["dsh.task"])
        self.plane.ingest_event(self.event)
        self.plane.create_conversation(conversation_id="conversation", kind="qq.group",
                                       participant_refs=["member"], policy_version="test-policy")
        self.plane.create_work_item(work_item_id="work", title="Review request", objective="Review it",
                                    source_event_ids=[self.event.event_id], completion_conditions=["Review complete"],
                                    risk_class="high", owner_ref="member")
        self.plane.propose_work_item("work")
        self.workflow = StubWorkflow()
        self.api = StubAPI()
        self.settings = QQBotSettings("app", "secret")
        self.service = QQChannelService(self.plane, self.workflow, self.api, self.settings)
        self.payload = {"id": "message", "group_openid": "group", "author": {"member_openid": "member"},
                        "content": "Please review this", "timestamp": "2026-11-15T00:00:00Z"}

    def tearDown(self) -> None:
        self.plane.close()
        self.directory.cleanup()

    def test_malformed_gateway_event_is_discarded_without_restarting_workflow(self) -> None:
        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", {"id": "broken"})
        self.assertEqual(self.workflow.calls, 0)
        self.assertEqual(self.plane.get_event(self.event.event_id), self.event)

    def test_qq_event_receives_a_scoped_knowledge_grant_before_agent_submission(self) -> None:
        class KnowledgeGrantStub:
            settings = SimpleNamespace(max_file_bytes=12 * 1024 * 1024)
            calls = 0

            def issue_grant(self, event: Event) -> str:
                self.calls += 1
                self.event = event
                return "g" * 40

        knowledge = KnowledgeGrantStub()
        self.payload = {**self.payload, "id": "knowledge-grant-message"}
        self.service = QQChannelService(self.plane, self.workflow, self.api, self.settings,
                                        knowledge_service=knowledge)

        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)

        self.assertEqual(self.workflow.events[0].knowledge_grant, "g" * 40)
        self.assertEqual(knowledge.calls, 1)
        self.assertEqual(knowledge.event.source_id, "group")

    def test_duplicate_qq_event_reuses_its_saved_knowledge_grant(self) -> None:
        class KnowledgeGrantStub:
            settings = SimpleNamespace(max_file_bytes=12 * 1024 * 1024)

            def __init__(self):
                self.calls = 0

            def issue_grant(self, _event: Event) -> str:
                self.calls += 1
                return f"grant-{self.calls}-" + "g" * 40

        class PersistingWorkflow:
            def __init__(self, plane):
                self.plane = plane
                self.events = []

            def process_event(self, event, *, workspace_id=None):
                self.events.append(event)
                self.plane.ingest_event(event)
                return SimpleNamespace(to_dict=lambda: {"status": "proposed"})

        knowledge = KnowledgeGrantStub()
        workflow = PersistingWorkflow(self.plane)
        self.payload = {**self.payload, "id": "replayed-knowledge-message"}
        self.service = QQChannelService(self.plane, workflow, self.api, self.settings,
                                        knowledge_service=knowledge)

        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)
        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)

        self.assertEqual(knowledge.calls, 2)
        self.assertEqual(len(workflow.events), 2)
        self.assertNotEqual(workflow.events[0].knowledge_grant, workflow.events[1].knowledge_grant)

    def test_configured_dsh_workspace_is_passed_to_the_qq_workflow(self) -> None:
        self.settings = QQBotSettings("app", "secret", auto_execute_capabilities=frozenset({"dsh.task"}),
                                      workspace_id="qq-agent")
        self.service = QQChannelService(self.plane, self.workflow, self.api, self.settings)

        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)

        self.assertEqual(self.workflow.workspace_ids, ["qq-agent"])

    def test_publication_is_suppressed_by_default_and_only_saved_as_decision(self) -> None:
        self.workflow.publication = {"recommendation": "group_update", "reason": "Useful", "body": "Done"}
        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)
        import hashlib
        decision_id = "qq-publication-" + hashlib.sha256(self.event.event_id.encode("utf-8")).hexdigest()[:24]
        audit = self.plane.list_audit(entity_type="publication_decision", entity_id=decision_id)
        self.assertEqual(audit[-1].payload["disposition"], "suppress")
        self.assertEqual(self.plane.list_outbox(), [])
        self.assertEqual(self.api.sent, [])

    def test_observed_run_can_publish_a_requested_status_message(self) -> None:
        self.settings = QQBotSettings("app", "secret", frozenset({"group_update"}))
        self.service = QQChannelService(self.plane, self.workflow, self.api, self.settings)
        self.workflow.publication = {"recommendation": "group_update", "reason": "Need to report a blocker",
                                     "body": "The requested file is not available yet."}
        self.workflow.run_data = {"status": "observed", "publication": self.workflow.publication,
                                  "work_item_id": None, "execution_results": []}

        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)

        self.assertEqual(len(self.api.sent), 1)
        self.assertEqual(self.api.sent[0].body, "The requested file is not available yet.")
        self.assertEqual(self.plane.list_outbox()[0].status, "sent")

    def test_allowlisted_publication_uses_outbox_and_delivery_is_idempotent(self) -> None:
        self.settings = QQBotSettings("app", "secret", frozenset({"group_update"}))
        self.service = QQChannelService(self.plane, self.workflow, self.api, self.settings)
        self.workflow.publication = {"recommendation": "group_update", "reason": "Complete", "body": "Review complete"}
        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)
        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)
        outbox = self.plane.list_outbox()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0].status, "sent")
        self.assertEqual(outbox[0].destination["reply_to_message_id"], "message")
        self.assertEqual(len(self.api.sent), 1)

    def test_observed_group_update_is_sent_without_a_work_item(self) -> None:
        self.settings = QQBotSettings("app", "secret", frozenset({"group_update"}))
        self.service = QQChannelService(self.plane, self.workflow, self.api, self.settings)
        self.workflow.publication = {
            "recommendation": "group_update",
            "reason": "The requested attachment failed to index",
            "body": "The file could not be indexed. Please resend it.",
        }
        self.workflow.run_data = {"status": "observed", "work_item_id": None}

        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)

        outbox = self.plane.list_outbox()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0].status, "sent")
        self.assertEqual(self.api.sent[0].body, "The file could not be indexed. Please resend it.")

    def test_completed_run_without_user_reply_reports_contract_failure_not_execution_output(self) -> None:
        self.settings = QQBotSettings("app", "secret", frozenset({"group_update"}))
        self.service = QQChannelService(self.plane, self.workflow, self.api, self.settings)
        self.workflow.publication = {"recommendation": "group_update", "reason": "Complete",
                                     "body": "I will summarize it shortly."}
        self.workflow.run_data = {
            "status": "completed", "execution_results": [{"status": "succeeded", "output": "Maintenance ended at 18:00."}],
        }

        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)

        self.assertEqual(len(self.api.sent), 1)
        self.assertIn("没有生成可投递的最终答复", self.api.sent[0].body)
        self.assertNotIn("Maintenance ended at 18:00", self.api.sent[0].body)

    def test_completed_run_publishes_final_text_artifact_instead_of_execution_log(self) -> None:
        self.settings = QQBotSettings("app", "secret", frozenset({"group_update"}))
        self.service = QQChannelService(self.plane, self.workflow, self.api, self.settings)
        manager = WorkspaceManager(Path(self.directory.name) / "workspaces")
        workspace = manager.provision("qq-agent")
        execution_id = "execution-1"
        (workspace.root / "artifacts" / "step-02-result.md").write_text(
            "执行说明：此段不能发送。\n\n"
            "<!-- COMPANY_AGENT_FINAL_REPLY_BEGIN:execution-1 -->\n"
            "# Group summary\n\nThe change window is 02:00-04:30.\n"
            "<!-- COMPANY_AGENT_FINAL_REPLY_END:execution-1 -->\n\n"
            "内部状态：此段也不能发送。", encoding="utf-8"
        )
        self.workflow.manager = manager
        self.workflow.publication = {"recommendation": "group_update", "reason": "Complete", "body": "Working"}
        self.workflow.run_data = {
            "status": "completed", "workspace_id": "qq-agent", "execution_results": [{
                "execution_id": execution_id, "status": "succeeded", "output": "Task completed; artifact written.",
                "artifact_refs": ["artifacts/step-02-result.md"],
            }],
        }

        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)

        self.assertEqual(self.api.sent[0].body,
                         "处理完成：\n# Group summary\n\nThe change window is 02:00-04:30.")

    def test_proposed_run_does_not_publish_an_acknowledgement_as_a_result(self) -> None:
        self.settings = QQBotSettings("app", "secret", frozenset({"group_update"}))
        self.service = QQChannelService(self.plane, self.workflow, self.api, self.settings)
        self.workflow.publication = {"recommendation": "group_update", "reason": "Work proposed",
                                     "body": "I will summarize this file."}
        self.workflow.run_data = {"status": "proposed"}

        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)

        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.plane.list_outbox(), [])

    def test_failed_run_publishes_failure_stage(self) -> None:
        self.settings = QQBotSettings("app", "secret", frozenset({"group_update"}))
        self.service = QQChannelService(self.plane, self.workflow, self.api, self.settings)
        self.workflow.publication = {"recommendation": "group_update", "reason": "Failed", "body": "Working"}
        self.workflow.run_data = {"status": "execution_failed", "error": "attachment could not be read"}

        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)

        self.assertIn("attachment could not be read", self.api.sent[0].body)

    def test_dssuperagent_timeout_is_reported_without_dropping_the_qq_event(self) -> None:
        self.settings = QQBotSettings("app", "secret", frozenset({"group_update"}))

        class TimeoutUpstream:
            def submit_event(self, event, *, context=None):
                return DssuperAgentReceipt("stored", f"company-agent:{event.event_id}",
                                           f"company-agent:{event.conversation_id}")

            def wait_for_decision(self, _thread_key, _trigger_id, *, timeout=600):
                raise DssuperAgentTimeout("provider failed with secret-value")

        workflow = DssuperAgentWorkflow(self.plane, TimeoutUpstream(), policy=AssistantPolicy())
        self.service = QQChannelService(self.plane, workflow, self.api, self.settings)
        self.payload = {**self.payload, "id": "timeout-message"}

        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)

        failed_run = self.plane.get_assistant_run("qq:app:group:timeout-message")
        self.assertEqual(failed_run.error_category, "decision_timeout", failed_run.error)
        outbox = self.plane.list_outbox()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0].status, "sent")
        self.assertIn("未能完成", self.api.sent[0].body)
        self.assertNotIn("secret-value", self.api.sent[0].body)
        self.assertNotIn("secret-value", outbox[0].last_error or "")

    def test_revoked_allowlist_cancels_previously_queued_publication(self) -> None:
        decision = self.plane.decide_publication(
            decision_id="publication-queued", work_item_id="work", conversation_id="conversation",
            disposition="group_update", reason="Allowed at queue time", policy_version="test-policy",
            body="Update", idempotency_key="queued-revoke",
            destination={"channel": "qq", "group_openid": "group"},
        )
        self.assertIsNotNone(decision.outbox_id)
        self.service.flush_outbox()
        message = self.plane.list_outbox()[0]
        self.assertEqual(message.status, "cancelled")
        self.assertEqual(self.api.sent, [])

    def test_sending_message_has_an_audited_manual_resolution_path(self) -> None:
        decision = self.plane.decide_publication(
            decision_id="publication-uncertain", work_item_id="work", conversation_id="conversation",
            disposition="group_update", reason="Manual resolution test", policy_version="test-policy",
            body="Update", idempotency_key="uncertain-resolution",
            destination={"channel": "qq", "group_openid": "group"},
        )
        message = self.plane.mark_outbox_attempt(decision.outbox_id)
        requeued = self.plane.resolve_outbox_delivery(message.outbox_id, delivered=False)
        self.assertEqual(requeued.status, "pending")
        sending = self.plane.mark_outbox_attempt(message.outbox_id)
        resolved = self.plane.resolve_outbox_delivery(sending.outbox_id, delivered=True)
        self.assertEqual(resolved.status, "sent")
        audit = self.plane.list_audit(entity_type="outbox", entity_id=message.outbox_id)
        self.assertEqual([item.action for item in audit[-3:]], ["manually_requeued", "sending", "manually_confirmed_sent"])

    def test_delivery_failure_is_recorded_and_retried(self) -> None:
        self.settings = QQBotSettings("app", "secret", frozenset({"urgent_escalation"}))
        self.service = QQChannelService(self.plane, self.workflow, self.api, self.settings)
        self.workflow.publication = {"recommendation": "urgent_escalation", "reason": "Urgent", "body": "Check now"}
        self.api.fail = True
        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)
        failed = self.plane.list_outbox()[0]
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.attempts, 1)
        self.api.fail = False
        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)
        retried = self.plane.list_outbox()[0]
        self.assertEqual(retried.status, "sent")
        self.assertEqual(retried.attempts, 2)
        self.assertEqual(len(self.api.sent), 1)


class QQApiTests(unittest.TestCase):
    def test_gateway_url_adds_protocol_query_and_group_send_uses_destination(self) -> None:
        api = QQBotAPI(QQBotSettings("app", "secret"))
        api._request = Mock(side_effect=[{"access_token": "token", "expires_in": 7200},
                                  {"url": "wss://gateway.example.test"}])
        self.assertEqual(api.gateway_url(), "wss://gateway.example.test?v=1&encoding=json")
        self.assertEqual(api._request.call_args_list[0].args[:2],
                         ("POST", "https://bots.qq.com/app/getAppAccessToken"))
        api.access_token = Mock(return_value="access-token")
        api._request = Mock(return_value={})
        message = OutboxMessage("outbox-1", "group-1", "Update", "key", "sending", "now",
                                {"channel": "qq", "group_openid": "group/a", "reply_to_message_id": "msg-1",
                                 "reply_deadline_epoch": "2000000000"})
        with unittest.mock.patch("app.qq_channel.time.time", return_value=1999999999):
            api.send_group_message(message)
        _args, kwargs = api._request.call_args
        self.assertEqual(kwargs["body"], {
            "msg_type": 0, "content": "Update", "msg_id": "msg-1", "msg_seq": 1,
        })

        with unittest.mock.patch("app.qq_channel.time.time", return_value=2000000000):
            api.send_group_message(message)
        args, kwargs = api._request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://api.bot.qq.com/v2/groups/group%2Fa/messages")
        self.assertEqual(kwargs["body"], {"msg_type": 0, "content": "Update"})
        self.assertEqual(kwargs["token"], "access-token")

    def test_group_passive_reply_rejects_sequence_above_platform_limit(self) -> None:
        api = QQBotAPI(QQBotSettings("app", "secret"))
        api.access_token = Mock(return_value="access-token")
        api._request = Mock(return_value={})
        message = OutboxMessage("outbox-1", "group-1", "Update", "key", "sending", "now",
                                {"channel": "qq", "group_openid": "group-1", "reply_to_message_id": "msg-1",
                                 "reply_deadline_epoch": "2000000000", "msg_seq": "6"})

        with self.assertRaisesRegex(ValueError, "at most five passive replies"):
            api.send_group_message(message)

        api._request.assert_not_called()

class QQGatewayTests(unittest.TestCase):
    def test_identify_requests_group_messages_only_and_resume_uses_saved_cursor(self) -> None:
        identify = QQGateway._identify("token")
        self.assertEqual(identify, {"op": 2, "d": {"token": "QQBot token", "intents": 1 << 25, "shard": [0, 1]}})
        gateway = QQGateway(StubAPI(), lambda _type, _payload: None)
        gateway.session_id, gateway.sequence = "session", 42
        self.assertEqual(gateway._resume("token"), {"op": 6, "d": {"token": "QQBot token",
                         "session_id": "session", "seq": 42}})

    def test_slow_event_handler_does_not_block_gateway_reader(self) -> None:
        import asyncio
        import threading

        started = threading.Event()
        release = threading.Event()

        def slow_handler(_event_type, _payload):
            started.set()
            release.wait(3)

        gateway = QQGateway(StubAPI(), slow_handler)

        async def exercise():
            task = gateway._dispatch_event("GROUP_MESSAGE_CREATE", {})
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            self.assertFalse(task.done())
            release.set()
            await task

        asyncio.run(exercise())


class QQAttachmentPipelineTests(unittest.TestCase):
    def test_original_is_stored_before_agent_and_selected_attachment_is_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plane = ControlPlane.open(root / "control.sqlite")
            manager = WorkspaceManager(root / "workspaces")
            settings = KnowledgeSettings("http://127.0.0.1:3001", "key", "g" * 40,
                                         frozenset(), parse_timeout_seconds=1)

            class Client:
                def ensure_workspace(self, slug: str) -> str:
                    return slug

                def upload_document(self, path: Path, filename: str, *, workspaces: list[str],
                                    source_hash: str, parser_extension: str | None = None) -> str:
                    return f"custom-documents/{source_hash}.txt"

                def search(self, *_args, **_kwargs):
                    return []

            knowledge = KnowledgeService(plane.store, settings, root / "knowledge-originals", Client())
            delivery = KnowledgeDelivery(knowledge, manager)

            class Upstream:
                def __init__(self) -> None:
                    self.submitted = None
                    self.results = []

                def submit_event(self, event, *, context=None):
                    self.submitted = event
                    return DssuperAgentReceipt("stored", f"trigger:{event.event_id}",
                                               f"thread:{event.conversation_id}")

                def wait_for_decision(self, _thread_key, _trigger_id, *, timeout=120):
                    attachment_id = self.submitted.attachments[0]["attachment_id"]
                    return DssuperAgentDecision(
                        decision_id="decision-pipeline", trigger_ids=[f"trigger:{self.submitted.event_id}"],
                        thread_key=f"thread:{self.submitted.conversation_id}", kind="ignore", outcome="ignore",
                        reason="The request needs the received attachment", occurred_at=self.submitted.occurred_at,
                        strength="inferred", assistant_intent={
                            "action": "propose_work", "rationale": "Use the selected attachment",
                            "title": "Inspect attachment", "objective": "Return a checked result",
                            "completion_conditions": ["A result is saved"], "plan_steps": [{
                                "title": "Inspect selected file", "objective": "Read the selected original",
                                "capability": "dsh.task", "completion_conditions": ["The file is inspected"],
                                "attachment_ids": [attachment_id],
                            }], "publication": {"recommendation": "suppress", "reason": "test"},
                        })

                def submit_execution_result(self, event, *, context=None):
                    self.results.append(event)
                    return DssuperAgentReceipt("stored", f"result:{event.event_id}",
                                               f"thread:{event.conversation_id}")

            upstream = Upstream()
            captured = {}

            class Dispatcher:
                knowledge_service = knowledge

                def dispatch(self, execution_id: str):
                    request = plane.get_execution(execution_id)
                    workspace = manager.get(request.workspace_id)
                    work_item = plane.get_work_item(request.work_item_id)
                    source_event = plane.get_event(work_item.source_event_ids[0])
                    task_files = delivery.stage(source_event, workspace, execution_id, [],
                                                list(request.parameters["attachment_ids"]))
                    captured["files"] = [(item.filename, (workspace.directory("work") / item.relative_path).read_bytes())
                                          for item in task_files]
                    delivery.cleanup(workspace, execution_id)
                    return ExecutionResult(execution_id, "succeeded", artifact_refs=[])

            from app.assistant_workflow import AssistantPolicy, DssuperAgentWorkflow
            workflow = DssuperAgentWorkflow(
                plane, upstream, manager=manager, dispatcher=Dispatcher(), knowledge_service=knowledge,
                policy=AssistantPolicy(auto_execute_capabilities=frozenset({"dsh.task"})),
            )
            ingestor = AttachmentIngestor(plane.store, knowledge_service=knowledge,
                                          storage_directory=root / "attachments")
            ingestor._download_to = lambda _url, destination: (destination.write_bytes(b"selected original") or 16)  # type: ignore[method-assign]
            api = StubAPI()
            service = QQChannelService(plane, workflow, api,
                                       QQBotSettings("app", "secret", auto_execute_capabilities=frozenset({"dsh.task"}),
                                                     workspace_id="qq-agent"),
                                       attachment_ingestor=ingestor, knowledge_service=knowledge)

            prepared = service.prepare_gateway_event("GROUP_MESSAGE_CREATE", {
                "id": "pipeline-message", "group_openid": "group", "author": {"member_openid": "member"},
                "content": "请检查这个附件", "timestamp": "2026-11-17T00:00:00Z",
                "attachments": [{"filename": "note.txt", "content_type": "text/plain",
                                 "url": "https://cdn.example/note"}],
            })
            self.assertIsNotNone(prepared)
            event, destination = prepared
            event = ingestor.prepare_originals(event, on_update=plane.update_event)
            def fail_fast(_event, _destination, category):
                raise RuntimeError(f"processing failure: {category}")
            service._publish_processing_failure = fail_fast
            service.process_prepared_event(event, destination)
            service.process_prepared_attachments(event)
            service.process_prepared_attachments(event)

            self.assertIsNotNone(upstream.submitted)
            self.assertEqual(upstream.submitted.attachments[0]["status"], "stored")
            self.assertEqual(captured.get("files"), [("note.txt", b"selected original")])
            saved = plane.get_event("qq:app:group:pipeline-message")
            self.assertEqual(saved.attachments[0]["status"], "ready")
            self.assertEqual(plane.store.connection.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0], 1)
            plane.close()

class StubWorkflow:
    publication = None

    def __init__(self):
        self.calls = 0
        self.events = []
        self.workspace_ids = []

    def process_event(self, _event, *, workspace_id=None):
        self.calls += 1
        self.events.append(_event)
        self.workspace_ids.append(workspace_id)
        value = {"status": "completed", "publication": self.publication, "work_item_id": "work",
                 "conversation_id": "conversation", "policy_version": "test-policy"}
        value.update(getattr(self, "run_data", {}))
        return SimpleNamespace(to_dict=lambda: value)


class StubAPI:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send_group_message(self, message):
        if self.fail:
            raise RuntimeError("mock QQ outage")
        self.sent.append(message)


if __name__ == "__main__":
    unittest.main()
