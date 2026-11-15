from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.control_plane import ControlPlane
from app.models import Event
from app.qq_channel import QQBotAPI, QQChannelService, QQGateway, normalize_group_message
from app.models import OutboxMessage
from app.runtime_config import QQBotSettings


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
                                       "reply_to_message_id": "message-123"})

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

    def test_publication_is_suppressed_by_default_and_only_saved_as_decision(self) -> None:
        self.workflow.publication = {"recommendation": "group_update", "reason": "Useful", "body": "Done"}
        self.service.handle_gateway_event("GROUP_MESSAGE_CREATE", self.payload)
        import hashlib
        decision_id = "qq-publication-" + hashlib.sha256(self.event.event_id.encode("utf-8")).hexdigest()[:24]
        audit = self.plane.list_audit(entity_type="publication_decision", entity_id=decision_id)
        self.assertEqual(audit[-1].payload["disposition"], "suppress")
        self.assertEqual(self.plane.list_outbox(), [])
        self.assertEqual(self.api.sent, [])

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
        api.access_token = Mock(return_value="access-token")
        api._request = Mock(return_value={})
        message = OutboxMessage("outbox-1", "group-1", "Update", "key", "sending", "now",
                                {"channel": "qq", "group_openid": "group/a", "reply_to_message_id": "msg-1"})
        api.send_group_message(message)
        args, kwargs = api._request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://api.sgroup.qq.com/v2/groups/group%2Fa/messages")
        self.assertEqual(kwargs["body"], {"msg_type": 0, "content": "Update", "msg_id": "msg-1"})
        self.assertEqual(kwargs["token"], "access-token")

class QQGatewayTests(unittest.TestCase):
    def test_identify_requests_group_messages_only_and_resume_uses_saved_cursor(self) -> None:
        identify = QQGateway._identify("token")
        self.assertEqual(identify, {"op": 2, "d": {"token": "QQBot token", "intents": 1 << 25, "shard": [0, 1]}})
        gateway = QQGateway(StubAPI(), lambda _type, _payload: None)
        gateway.session_id, gateway.sequence = "session", 42
        self.assertEqual(gateway._resume("token"), {"op": 6, "d": {"token": "QQBot token",
                         "session_id": "session", "seq": 42}})


class StubWorkflow:
    publication = None

    def __init__(self):
        self.calls = 0

    def process_event(self, _event):
        self.calls += 1
        return SimpleNamespace(to_dict=lambda: {"publication": self.publication, "work_item_id": "work",
                                                "conversation_id": "conversation", "policy_version": "test-policy"})


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
