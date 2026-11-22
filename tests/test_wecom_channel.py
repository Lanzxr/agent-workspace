from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.admin_api import make_handler
from app.assistant_workflow import AssistantPolicy, DssuperAgentWorkflow
from app.dsh import DshAdapter
from app.execution import ExecutionDispatcher
from app.execution_state import ExecutionBusy
from app.attachments import (
    AttachmentIngestor,
    _parse_content_disposition_filename,
    decrypt_wecom_media,
)
from app.control_plane import ControlPlane
from app.dssuperagent import DssuperAgentDecision, DssuperAgentReceipt
from app.identity import IdentityRegistry
from app.knowledge.config import KnowledgeSettings
from app.knowledge.service import KnowledgeService
from app.mcp_registry import _RESERVED_PLATFORM_ENV_VARS
from app.models import (
    Event,
    ExecutionResult,
    OutboxMessage,
    is_direct_source,
    is_group_source,
    is_im_source,
    source_identity_scope,
    source_provider,
)
from app.observability import list_conversations
from app.runtime_config import RuntimeConfig, WeComBotSettings
from app.scheduled_resume import ScheduledResumeHandler
from app.scheduler import ScheduledTask
from app.wecom_channel import (
    WeComBotAPI,
    WeComChannelService,
    WeComGateway,
    normalize_wecom_message,
)
from app.workspace import WorkspaceManager


def _encrypt_wecom_payload(plaintext: bytes, aes_key_b64: str, *, block_size: int = 32) -> bytes:
    padded_b64 = aes_key_b64 + "=" * (-len(aes_key_b64) % 4)
    key = base64.b64decode(padded_b64)
    iv = key[:16]
    pad_len = block_size - (len(plaintext) % block_size)
    padded = plaintext + bytes([pad_len] * pad_len)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


class WeComStubWorkflow:
    publication = None

    def __init__(self) -> None:
        self.calls = 0
        self.events: list[Event] = []
        self.workspace_ids: list[str | None] = []
        self.run_data: dict[str, object] = {}

    def process_event(self, event: Event, *, workspace_id: str | None = None):
        self.calls += 1
        self.events.append(event)
        self.workspace_ids.append(workspace_id)
        value = {
            "status": "completed",
            "publication": self.publication,
            "work_item_id": "work-wecom",
            "conversation_id": event.conversation_id,
            "policy_version": "test-policy",
        }
        value.update(self.run_data)
        return SimpleNamespace(to_dict=lambda: value)


class WeComStubAPI:
    def __init__(self) -> None:
        self.text_messages: list[OutboxMessage] = []
        self.media_messages: list[tuple[OutboxMessage, str, str]] = []
        self.welcome_replies: list[tuple[str, str]] = []
        self.fail = False

    def send_text_message(self, message: OutboxMessage) -> None:
        if self.fail:
            raise RuntimeError("mock WeCom outage")
        self.text_messages.append(message)

    def send_media_message(self, message: OutboxMessage, *, file_path: str, filename: str) -> None:
        if self.fail:
            raise RuntimeError("mock WeCom media outage")
        self.media_messages.append((message, file_path, filename))

    def send_welcome_reply(self, req_id: str, content: str) -> None:
        if self.fail:
            raise RuntimeError("mock WeCom welcome outage")
        self.welcome_replies.append((req_id, content))


class WeComConfigTests(unittest.TestCase):
    def test_is_configured_and_loads_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text("COMPANY_AGENT_ADMIN_TOKEN=tok\n", encoding="utf-8")
            self.assertFalse(WeComBotSettings.is_configured(path))

            path.write_text(
                "WECOM_BOT_ID=wb-001\nWECOM_BOT_SECRET=sec-xyz\n",
                encoding="utf-8",
            )
            self.assertTrue(WeComBotSettings.is_configured(path))
            settings = WeComBotSettings.load(path)
            self.assertEqual(settings.bot_id, "wb-001")
            self.assertEqual(settings.bot_secret, "sec-xyz")
            self.assertEqual(settings.channel_name, "wecom")
            self.assertEqual(settings.account_id, "wb-001")
            self.assertEqual(settings.ws_url, "wss://openws.work.weixin.qq.com")
            self.assertEqual(settings.allowed_publications, frozenset())

            path.write_text(
                "COMPANY_AGENT_WECOM_BOT_ID=wb-002\n"
                "COMPANY_AGENT_WECOM_BOT_SECRET=sec-alias\n"
                "WECOM_ALLOWED_PUBLICATIONS=group_update,urgent_escalation\n"
                "WECOM_AUTO_EXECUTE_CAPABILITIES=dsh.task\n"
                "COMPANY_AGENT_WECOM_WORKSPACE=wecom-ws\n",
                encoding="utf-8",
            )
            aliased = WeComBotSettings.load(path)
            self.assertEqual(aliased.bot_id, "wb-002")
            self.assertEqual(aliased.bot_secret, "sec-alias")
            self.assertEqual(aliased.allowed_publications, frozenset({"group_update", "urgent_escalation"}))
            self.assertEqual(aliased.workspace_id, "wecom-ws")

    def test_validation_rejects_non_wss_or_missing_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text(
                "WECOM_BOT_ID=wb-1\nWECOM_BOT_SECRET=sec-1\nWECOM_WS_URL=ws://insecure.invalid\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "wss://"):
                WeComBotSettings.load(path)

            path.write_text(
                "WECOM_BOT_ID=wb-1\nWECOM_BOT_SECRET=sec-1\nWECOM_AUTO_EXECUTE_CAPABILITIES=dsh.task\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "COMPANY_AGENT_WECOM_WORKSPACE"):
                WeComBotSettings.load(path)

    def test_reserved_env_vars_and_forge_mcp_owner_include_wecom(self) -> None:
        self.assertIn("WECOM_BOT_SECRET", _RESERVED_PLATFORM_ENV_VARS)
        self.assertIn("COMPANY_AGENT_WECOM_BOT_SECRET", _RESERVED_PLATFORM_ENV_VARS)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text(
                "AGENT_MODEL_URL=https://example.invalid/v1\n"
                "AGENT_MODEL_API_KEY=sk-test\n"
                "AGENT_MODEL_NAME=deepseek-chat\n"
                "COMPANY_AGENT_FORGE_MCP_OWNER=wecom\n",
                encoding="utf-8",
            )
            cfg = RuntimeConfig.load(path)
            self.assertEqual(cfg.forge_mcp_owner, "wecom")


class WeComTaxonomyTests(unittest.TestCase):
    def test_source_taxonomy_helpers_handle_qq_and_wecom(self) -> None:
        self.assertTrue(is_group_source("wecom.group"))
        self.assertTrue(is_group_source("qq.group"))
        self.assertFalse(is_group_source("wecom.dm"))

        self.assertTrue(is_direct_source("wecom.dm"))
        self.assertTrue(is_direct_source("wecom.single"))
        self.assertTrue(is_direct_source("qq.c2c"))
        self.assertTrue(is_direct_source("qq.private"))

        for source_type in ("wecom.group", "wecom.dm", "wecom.single", "qq.group", "qq.c2c"):
            self.assertTrue(is_im_source(source_type))
        self.assertEqual(source_provider("wecom.group"), "wecom")
        self.assertEqual(source_provider("qq.c2c"), "qq")
        self.assertEqual(source_identity_scope("wecom.group", "g-1"), "g-1")
        self.assertEqual(source_identity_scope("wecom.dm", "u-1"), "*")


class WeComMessageNormalizationTests(unittest.TestCase):
    def test_single_dm_normalizes_without_chatid(self) -> None:
        frame = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-dm-1"},
            "body": {
                "msgid": "msg-dm-1",
                "aibotid": "wb-1",
                "chattype": "single",
                "from": {"userid": "zhangsan"},
                "create_time": 1794676023,
                "msgtype": "text",
                "text": {"content": "帮我查一下今天日程"},
            },
        }
        event, dest = normalize_wecom_message("wb-1", frame)
        self.assertEqual(event.event_id, "wecom:wb-1:dm:msg-dm-1")
        self.assertEqual(event.source_type, "wecom.dm")
        self.assertEqual(event.source_id, "zhangsan")
        self.assertEqual(event.conversation_id, "zhangsan")
        self.assertEqual(event.actor_id, "zhangsan")
        self.assertEqual(event.content, "帮我查一下今天日程")
        self.assertEqual(dest["channel"], "wecom")
        self.assertEqual(dest["chatid"], "zhangsan")
        self.assertEqual(dest["userid"], "zhangsan")
        self.assertEqual(dest["chat_type"], "1")
        self.assertEqual(dest["req_id"], "req-dm-1")

    def test_group_message_strips_bot_mention_before_slash_command(self) -> None:
        frame = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-grp-cmd"},
            "body": {
                "msgid": "msg-grp-1",
                "chattype": "group",
                "chatid": "wr-group-9",
                "from": {"userid": "admin-01"},
                "msgtype": "text",
                "text": {"content": "@CompanyBot   /mark-internal"},
            },
        }
        event, dest = normalize_wecom_message("wb-1", frame)
        self.assertEqual(event.event_id, "wecom:wb-1:group:msg-grp-1")
        self.assertEqual(event.source_type, "wecom.group")
        self.assertEqual(event.source_id, "wr-group-9")
        self.assertEqual(event.actor_id, "admin-01")
        self.assertEqual(event.content, "/mark-internal")
        self.assertEqual(dest["chatid"], "wr-group-9")
        self.assertEqual(dest["chat_type"], "2")

    def test_voice_mixed_and_quoted_media_messages(self) -> None:
        voice_frame = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-v"},
            "body": {
                "msgid": "msg-v1",
                "chattype": "single",
                "from": {"userid": "emp-1"},
                "msgtype": "voice",
                "voice": {"content": "把上周销售数据导出来"},
            },
        }
        v_event, _ = normalize_wecom_message("wb-1", voice_frame)
        self.assertEqual(v_event.content, "把上周销售数据导出来")

        mixed_quote_frame = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-mq"},
            "body": {
                "msgid": "msg-mq1",
                "chattype": "group",
                "chatid": "wr-1",
                "from": {"userid": "emp-2"},
                "msgtype": "mixed",
                "mixed": {
                    "msg_item": [
                        {"msgtype": "text", "text": {"content": "请对照图片和报价单检查"}},
                        {"msgtype": "image", "image": {"url": "https://wecom.test/img1", "aeskey": "k1"}},
                    ]
                },
                "quote": {
                    "msgtype": "file",
                    "file": {"url": "https://wecom.test/quote.pdf", "aeskey": "k2", "filename": "quote.pdf"},
                },
            },
        }
        mq_event, _ = normalize_wecom_message("wb-1", mixed_quote_frame)
        self.assertIn("请对照图片和报价单检查", mq_event.content)
        self.assertEqual(len(mq_event.attachments), 2)
        self.assertEqual(mq_event.attachments[0]["url"], "https://wecom.test/img1")
        self.assertEqual(mq_event.attachments[0]["decrypt_key"], "k1")
        self.assertEqual(mq_event.attachments[1]["url"], "https://wecom.test/quote.pdf")
        self.assertEqual(mq_event.attachments[1]["filename"], "quote.pdf")


class WeComCryptoAndAttachmentTests(unittest.TestCase):
    def test_decrypt_wecom_media_supports_32_byte_pkcs7_and_unpadded_b64_key(self) -> None:
        raw_key = hashlib.sha256(b"test-wecom-aes-key").digest()
        aes_key_b64 = base64.b64encode(raw_key).decode("ascii").rstrip("=")
        self.assertFalse(aes_key_b64.endswith("="))

        for plaintext in (b"hello wecom", b"A" * 32, b"PDF-binary-\x00\x01\x02" * 7):
            encrypted = _encrypt_wecom_payload(plaintext, aes_key_b64, block_size=32)
            decrypted = decrypt_wecom_media(encrypted, aes_key_b64)
            self.assertEqual(decrypted, plaintext)

        with self.assertRaisesRegex(ValueError, "decode to 32 bytes"):
            decrypt_wecom_media(b"0" * 32, "YWJjZGVm")
        with self.assertRaisesRegex(ValueError, "padding"):
            bad = _encrypt_wecom_payload(b"abc", aes_key_b64, block_size=32)
            decrypt_wecom_media(bad[:-1] + bytes([bad[-1] ^ 0xFF]), aes_key_b64)

    def test_attachment_ingestor_strips_decrypt_key_and_decrypts_downloaded_file(self) -> None:
        raw_key = hashlib.sha256(b"attachment-secret").digest()
        aes_key_b64 = base64.b64encode(raw_key).decode("ascii").rstrip("=")
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF"
        encrypted = _encrypt_wecom_payload(pdf_bytes, aes_key_b64, block_size=32)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plane = ControlPlane.open(root / "control.sqlite")
            try:
                ingestor = AttachmentIngestor(plane.store, storage_directory=root / "attachments")
                event = Event(
                    "wecom:wb-1:group:msg-att-1",
                    "wecom.group",
                    "wr-9",
                    "wr-9",
                    "user-1",
                    "2026-11-21T01:02:03Z",
                    "请查看报价单",
                    [
                        {
                            "content_type": "application/octet-stream",
                            "filename": "",
                            "url": "https://wecom.invalid/media/999",
                            "decrypt_key": aes_key_b64,
                            "decrypt_alg": "aes-256-cbc-pkcs7-32",
                        }
                    ],
                    ["dsh.task"],
                )
                registered = ingestor.register_event(event)
                self.assertNotIn("decrypt_key", registered.attachments[0])
                plane.ingest_event(registered)

                # Confirm database rows never persist decrypt_key
                row = plane.store.connection.execute(
                    "SELECT payload FROM attachments"
                ).fetchone()
                self.assertIsNotNone(row)
                decoded_att = plane.store.decode(row["payload"])
                self.assertNotIn(aes_key_b64, json.dumps(decoded_att))
                self.assertNotIn("decrypt_key", decoded_att)

                def fake_download_to(url: str, destination: Path) -> int:
                    destination.write_bytes(encrypted)
                    hdr_name = _parse_content_disposition_filename(
                        "attachment; filename*=UTF-8''%E6%8A%A5%E4%BB%B7%E5%8D%95.pdf"
                    )
                    ingestor._download_meta[str(destination)] = (
                        hdr_name,
                        "application/octet-stream",
                    )
                    return len(encrypted)

                ingestor._download_to = fake_download_to  # type: ignore[method-assign]
                prepared = ingestor.prepare_originals(registered)
                att = prepared.attachments[0]
                self.assertEqual(att["status"], "stored")
                self.assertEqual(att["filename"], "报价单.pdf")
                self.assertEqual(att["content_hash"], hashlib.sha256(pdf_bytes).hexdigest())
                self.assertNotIn("original_path", att)
                stored_row = plane.store.decode(
                    plane.store.connection.execute("SELECT payload FROM attachments").fetchone()["payload"]
                )
                self.assertEqual(Path(stored_row["original_path"]).read_bytes(), pdf_bytes)
            finally:
                plane.close()


class WeComBotAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.sent_frames: list[tuple[str, str, dict]] = []
        self.rpc_responses: dict[str, dict] = {}
        self.settings = WeComBotSettings("wb-1", "sec-1")
        self.api = WeComBotAPI(self.settings, timeout=2.0)

        async def sender(cmd: str, req_id: str, body: dict, timeout: float) -> dict:
            self.sent_frames.append((cmd, req_id, body))
            if cmd in self.rpc_responses:
                resp = self.rpc_responses[cmd]
                return resp(cmd, req_id, body) if callable(resp) else resp
            return {"errcode": 0, "errmsg": "ok"}

        self.api.attach_transport(self.loop, sender)

    def tearDown(self) -> None:
        self.api.detach_transport()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()

    def test_passive_ack_then_active_final_push(self) -> None:
        now_epoch = str(time.time())
        ack_msg = OutboxMessage(
            outbox_id="out-ack",
            conversation_id="wr-100",
            body="收到，正在处理中",
            idempotency_key="idem-ack",
            status="queued",
            created_at="2026-11-21T00:00:00Z",
            destination={
                "channel": "wecom",
                "chatid": "wr-100",
                "chat_type": "2",
                "req_id": "req-msg-1",
                "received_at_epoch": now_epoch,
            },
        )
        self.api.send_text_message(ack_msg)
        self.assertEqual(len(self.sent_frames), 1)
        cmd, req_id, body = self.sent_frames[0]
        self.assertEqual(cmd, "aibot_respond_msg")
        self.assertEqual(req_id, "req-msg-1")
        self.assertEqual(body["msgtype"], "stream")
        self.assertTrue(body["stream"]["finish"])
        self.assertEqual(body["stream"]["content"], "收到，正在处理中")

        # Second reply sharing the same req_id MUST switch to active aibot_send_msg
        final_msg = OutboxMessage(
            outbox_id="out-final",
            conversation_id="wr-100",
            body="处理完成，详见结论",
            idempotency_key="idem-final",
            status="queued",
            created_at="2026-11-21T00:00:05Z",
            destination={
                "channel": "wecom",
                "chatid": "wr-100",
                "chat_type": "2",
                "req_id": "req-msg-1",
                "received_at_epoch": now_epoch,
            },
        )
        self.api.send_text_message(final_msg)
        self.assertEqual(len(self.sent_frames), 2)
        cmd2, _, body2 = self.sent_frames[1]
        self.assertEqual(cmd2, "aibot_send_msg")
        self.assertEqual(body2["chatid"], "wr-100")
        self.assertEqual(body2["msgtype"], "markdown")
        self.assertEqual(body2["markdown"]["content"], "处理完成，详见结论")

    def test_expired_or_rejected_passive_falls_back_to_active_send(self) -> None:
        self.rpc_responses["aibot_respond_msg"] = {"errcode": 99999, "errmsg": "req expired"}
        msg = OutboxMessage(
            outbox_id="out-fb",
            conversation_id="zhangsan",
            body="fallback active message",
            idempotency_key="idem-fb",
            status="queued",
            created_at="2026-11-21T00:00:00Z",
            destination={
                "channel": "wecom",
                "chatid": "zhangsan",
                "chat_type": "1",
                "req_id": "req-reject",
                "received_at_epoch": str(time.time()),
            },
        )
        self.api.send_text_message(msg)
        self.assertEqual([c for c, _, _ in self.sent_frames], ["aibot_respond_msg", "aibot_send_msg"])
        self.assertEqual(self.sent_frames[1][2]["chatid"], "zhangsan")

    def test_upload_media_uses_zero_based_chunks_and_sends_image_or_file(self) -> None:
        self.rpc_responses["aibot_upload_media_init"] = {"errcode": 0, "body": {"upload_id": "up-xyz"}}
        self.rpc_responses["aibot_upload_media_finish"] = {"errcode": 0, "body": {"media_id": "media-123"}}

        with tempfile.TemporaryDirectory() as directory:
            # 600 KiB payload triggers 2 chunks (0 and 1 at 512 KiB chunk size)
            report = Path(directory) / "summary.xlsx"
            payload = b"X" * (600 * 1024)
            report.write_bytes(payload)

            msg = OutboxMessage(
                outbox_id="out-media",
                conversation_id="wr-200",
                body="",
                idempotency_key="idem-media",
                status="queued",
                created_at="2026-11-21T00:00:00Z",
                destination={"channel": "wecom", "chatid": "wr-200", "chat_type": "2"},
            )
            self.api.send_media_message(msg, file_path=str(report), filename="summary.xlsx")

        cmds = [c for c, _, _ in self.sent_frames]
        self.assertEqual(
            cmds,
            [
                "aibot_upload_media_init",
                "aibot_upload_media_chunk",
                "aibot_upload_media_chunk",
                "aibot_upload_media_finish",
                "aibot_send_msg",
            ],
        )
        init_body = self.sent_frames[0][2]
        self.assertEqual(init_body["type"], "file")
        self.assertEqual(init_body["filename"], "summary.xlsx")
        self.assertEqual(init_body["total_size"], len(payload))
        self.assertEqual(init_body["total_chunks"], 2)
        self.assertEqual(init_body["md5"], hashlib.md5(payload).hexdigest())
        self.assertEqual(self.sent_frames[1][2]["chunk_index"], 0)
        self.assertEqual(self.sent_frames[2][2]["chunk_index"], 1)
        self.assertEqual(self.sent_frames[3][2]["upload_id"], "up-xyz")
        self.assertEqual(self.sent_frames[4][2]["msgtype"], "file")
        self.assertEqual(self.sent_frames[4][2]["file"]["media_id"], "media-123")

        # Image <= 10 MiB uses msgtype="image"
        self.sent_frames.clear()
        with tempfile.TemporaryDirectory() as directory:
            png = Path(directory) / "chart.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
            msg = OutboxMessage(
                outbox_id="out-img",
                conversation_id="u-1",
                body="",
                idempotency_key="idem-img",
                status="queued",
                created_at="2026-11-21T00:00:00Z",
                destination={"channel": "wecom", "chatid": "u-1", "chat_type": "1"},
            )
            self.api.send_media_message(msg, file_path=str(png), filename="chart.png")
        self.assertEqual(self.sent_frames[0][2]["type"], "image")
        self.assertEqual(self.sent_frames[-1][2]["msgtype"], "image")
        self.assertEqual(self.sent_frames[-1][2]["image"]["media_id"], "media-123")

    def test_welcome_reply_uses_passive_welcome_cmd(self) -> None:
        self.api.send_welcome_reply("req-enter-1", "欢迎使用企业助手")
        cmd, req_id, body = self.sent_frames[-1]
        self.assertEqual(cmd, "aibot_respond_welcome_msg")
        self.assertEqual(req_id, "req-enter-1")
        self.assertEqual(body["msgtype"], "text")
        self.assertEqual(body["text"]["content"], "欢迎使用企业助手")


class WeComChannelServiceIsolationAndCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plane = ControlPlane.open(self.root / "control.sqlite")
        self.manager = WorkspaceManager(self.root / "workspaces")
        self.identities = IdentityRegistry(self.plane.store)
        self.api = WeComStubAPI()
        self.workflow = WeComStubWorkflow()
        self.workflow.manager = self.manager
        self.settings = WeComBotSettings(
            "wb-1",
            "sec-1",
            allowed_publications=frozenset({"group_update"}),
            auto_execute_capabilities=frozenset({"dsh.task"}),
            workspace_id="wecom-files",
        )

        class FakeLLM:
            def ensure_workspace(self, _slug): return "w"
            def upload_document(self, _path, _name, **_kwargs): return "doc-loc-1"
            def upload_text_file(self, _w, _p, *, source_name): return "d"
            def upload_binary_file(self, _w, _p, *, source_name, content_type): return "d"
            def search(self, _w, _q, *, limit=8): return []

        k_settings = KnowledgeSettings(
            "http://127.0.0.1", "k", "s" * 40,
            admin_user_ids=frozenset({"env-wecom-admin"}),
        )
        self.knowledge = KnowledgeService(
            self.plane.store, k_settings, self.root / "knowledge", FakeLLM(), identities=self.identities,
        )
        self.service = WeComChannelService(
            self.plane,
            self.workflow,
            self.api,
            self.settings,
            identities=self.identities,
            knowledge_service=self.knowledge,
            workspace_manager=self.manager,
        )

    def tearDown(self) -> None:
        self.service.close()
        self.plane.close()
        self.temp.cleanup()

    def test_group_isolation_slash_commands_and_forge_dm_boundary(self) -> None:
        admin_user = self.identities.create_user("WeCom Admin", role="admin")
        emp_user = self.identities.create_user("WeCom Employee", role="employee")
        admin_code = self.identities.create_bind_code(admin_user.user_id)["code"]
        emp_code = self.identities.create_bind_code(emp_user.user_id)["code"]

        # Two separate customer groups auto-register as L1 client_group with distinct workspaces
        self.service.handle_gateway_frame({
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r-c1"},
            "body": {
                "msgid": "m-c1", "chattype": "group", "chatid": "wc-grp-1",
                "from": {"userid": "cust-1"}, "msgtype": "text", "text": {"content": "客户1需求"},
            },
        })
        self.service.handle_gateway_frame({
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r-c2"},
            "body": {
                "msgid": "m-c2", "chattype": "group", "chatid": "wc-grp-2",
                "from": {"userid": "cust-2"}, "msgtype": "text", "text": {"content": "客户2需求"},
            },
        })
        g1 = self.identities.get_group("wecom.group", "wc-grp-1")
        g2 = self.identities.get_group("wecom.group", "wc-grp-2")
        self.assertIsNotNone(g1)
        self.assertIsNotNone(g2)
        self.assertEqual(g1.tier, "client_group")
        self.assertEqual(g2.tier, "client_group")
        self.assertTrue(self.workflow.workspace_ids[0].startswith("ws-client-"))
        self.assertTrue(self.workflow.workspace_ids[1].startswith("ws-client-"))
        self.assertNotEqual(self.workflow.workspace_ids[0], self.workflow.workspace_ids[1])

        # Employee binds via WeCom DM
        self.service.handle_gateway_frame({
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r-bind-emp"},
            "body": {
                "msgid": "m-bind-emp", "chattype": "single",
                "from": {"userid": "wc-emp"}, "msgtype": "text", "text": {"content": f"/bind {emp_code}"},
            },
        })
        self.assertIn("绑定成功", self.api.text_messages[-1].body)

        # Admin binds via WeCom DM and promotes wc-grp-1 to L2 internal_group using @Bot /mark-internal
        self.service.handle_gateway_frame({
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r-bind-adm"},
            "body": {
                "msgid": "m-bind-adm", "chattype": "single",
                "from": {"userid": "wc-adm"}, "msgtype": "text", "text": {"content": f"/bind {admin_code}"},
            },
        })
        self.service.handle_gateway_frame({
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r-mark-int"},
            "body": {
                "msgid": "m-mark-int", "chattype": "group", "chatid": "wc-grp-1",
                "from": {"userid": "wc-adm"}, "msgtype": "text", "text": {"content": "@Assistant /mark-internal"},
            },
        })
        self.assertEqual(self.identities.get_group("wecom.group", "wc-grp-1").tier, "internal")

        # /internal works in L2 group wc-grp-1, fails in L1 group wc-grp-2
        self.service.handle_gateway_frame({
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r-int-ok"},
            "body": {
                "msgid": "m-int-ok", "chattype": "group", "chatid": "wc-grp-1",
                "from": {"userid": "wc-emp"}, "msgtype": "text", "text": {"content": "/internal 内部会议纪要"},
            },
        })
        self.assertIn("已存入企业内部共享知识库", self.api.text_messages[-1].body)

        self.service.handle_gateway_frame({
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r-int-deny"},
            "body": {
                "msgid": "m-int-deny", "chattype": "group", "chatid": "wc-grp-2",
                "from": {"userid": "wc-adm"}, "msgtype": "text", "text": {"content": "/internal 不该泄密"},
            },
        })
        self.assertIn("客户群", self.api.text_messages[-1].body)

        # /forge* is strictly blocked inside any WeCom group, even L2, and allowed in L3 admin DM
        self.service.handle_gateway_frame({
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r-forge-grp"},
            "body": {
                "msgid": "m-forge-grp", "chattype": "group", "chatid": "wc-grp-1",
                "from": {"userid": "wc-adm"}, "msgtype": "text", "text": {"content": "/forge"},
            },
        })
        self.assertIn("私聊", self.api.text_messages[-1].body)

        self.service.handle_gateway_frame({
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r-forge-dm"},
            "body": {
                "msgid": "m-forge-dm", "chattype": "single",
                "from": {"userid": "wc-adm"}, "msgtype": "text", "text": {"content": "/forge"},
            },
        })
        self.assertIn("Tool Forge", self.api.text_messages[-1].body)

    def test_enter_chat_welcome_and_ambient_vs_direct_group_mode(self) -> None:
        self.service.handle_event_callback({
            "cmd": "aibot_event_callback",
            "headers": {"req_id": "req-welcome-1"},
            "body": {
                "aibotid": "wb-1",
                "from": {"userid": "new-emp"},
                "event": {"eventtype": "enter_chat"},
            },
        })
        self.assertEqual(len(self.api.welcome_replies), 1)
        self.assertEqual(self.api.welcome_replies[0][0], "req-welcome-1")

        # Ambient mode (`group_direct_on_callback=False`): plain text goes through triage, @Bot skips triage as direct
        recorded: list[tuple[str, str]] = []

        class StubJudgeWorkflow(DssuperAgentWorkflow):
            def __init__(self, plane: ControlPlane) -> None:
                super().__init__(plane, None, policy=AssistantPolicy(auto_execute_capabilities=frozenset({"dsh.task"})))

            def process_event(self, event: Event, *, workspace_id: str | None = None, interaction_mode: str = "direct"):
                recorded.append((event.event_id, interaction_mode))
                return SimpleNamespace(to_dict=lambda: {"status": "observed"})

        class FakeAdapter:
            def classify_message(self, **_kwargs):
                return {"classification": "chatter"}

        settings_ambient = WeComBotSettings("wb-1", "sec-1", group_direct_on_callback=False)
        plane2 = ControlPlane.open(self.root / "ambient.sqlite")
        try:
            wf2 = StubJudgeWorkflow(plane2)
            wf2.upstream = FakeAdapter()
            svc2 = WeComChannelService(
                plane2, wf2, self.api, settings_ambient,
                identities=IdentityRegistry(plane2.store), workspace_manager=self.manager,
            )
            svc2.ambient_cooldown_seconds = lambda: 0.05  # type: ignore[method-assign]
            # Chatter without @ is dropped before heavy workflow
            svc2.handle_gateway_frame({
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "req-chatter"},
                "body": {
                    "msgid": "m-chatter", "chattype": "group", "chatid": "wc-amb",
                    "from": {"userid": "u1"}, "msgtype": "text", "text": {"content": "收到哈哈"},
                },
            })
            self.assertEqual(recorded, [])
            # @mention goes directly as direct
            svc2.handle_gateway_frame({
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "req-at"},
                "body": {
                    "msgid": "m-at", "chattype": "group", "chatid": "wc-amb",
                    "from": {"userid": "u1"}, "msgtype": "text", "text": {"content": "@CompanyBot 查一下"},
                },
            })
            self.assertEqual(recorded, [("wecom:wb-1:group:m-at", "direct")])
            svc2.close()
        finally:
            plane2.close()


class WeComGatewayTests(unittest.TestCase):
    def test_gateway_subscribes_handles_callbacks_ping_and_disconnect(self) -> None:
        settings = WeComBotSettings("wb-gw", "sec-gw")
        api = WeComBotAPI(settings, timeout=2.0)
        statuses: list[str] = []
        seen_events: list[tuple[str, str]] = []
        stop = threading.Event()

        class GatewayService:
            def __init__(self) -> None:
                self.processed = threading.Event()
                self.plane = SimpleNamespace(update_event=lambda *_a, **_kw: None)
                self.attachment_ingestor = SimpleNamespace(
                    prepare_originals=lambda event, on_update=None: event,
                )

            def prepare_gateway_frame(self, frame: dict):
                normalized = normalize_wecom_message("wb-gw", frame)
                if normalized is not None:
                    seen_events.append((frame.get("cmd", ""), normalized[0].event_id))
                return normalized

            def process_prepared_event(self, event: Event, destination: dict) -> None:
                self.processed.set()

            def process_prepared_attachments(self, _event: Event) -> None:
                return None

            def handle_event_callback(self, frame: dict) -> None:
                ev_type = ((frame.get("body") or {}).get("event") or {}).get("eventtype", "")
                seen_events.append((frame.get("cmd", ""), ev_type))

        svc = GatewayService()
        gateway = WeComGateway(
            api,
            svc,
            reconnect_delay=0.05,
            heartbeat_interval=0.05,
            on_status=statuses.append,
        )

        ping_seen = threading.Event()

        class FakeWebSocket:
            def __init__(self, script: list[dict]) -> None:
                self.sent: list[dict] = []
                self._incoming: asyncio.Queue[str] = asyncio.Queue()
                self._script = list(script)
                self._sub_acked = False
                self._disc_sent = False

            async def send(self, raw: str) -> None:
                payload = json.loads(raw)
                self.sent.append(payload)
                cmd = payload.get("cmd")
                req_id = (payload.get("headers") or {}).get("req_id", "")
                if cmd in {"aibot_subscribe", "ping", "aibot_respond_msg", "aibot_send_msg"}:
                    await self._incoming.put(
                        json.dumps({"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"})
                    )
                if cmd == "aibot_subscribe" and not self._sub_acked:
                    self._sub_acked = True
                    for item in self._script[:2]:
                        await self._incoming.put(json.dumps(item))
                if cmd == "ping" and not self._disc_sent:
                    self._disc_sent = True
                    ping_seen.set()
                    await self._incoming.put(json.dumps(self._script[2]))

            async def recv(self) -> str:
                return await self._incoming.get()

            def __aiter__(self):
                return self

            async def __anext__(self) -> str:
                return await self._incoming.get()

            async def close(self) -> None:
                return None

        ws_conn = FakeWebSocket([
            {
                "cmd": "aibot_msg_callback",
                "headers": {"req_id": "req-cb-1"},
                "body": {
                    "msgid": "m-gw-1",
                    "chattype": "single",
                    "from": {"userid": "alice"},
                    "msgtype": "text",
                    "text": {"content": "hello ws"},
                },
            },
            {
                "cmd": "aibot_event_callback",
                "headers": {"req_id": "req-evt-1"},
                "body": {"event": {"eventtype": "enter_chat"}, "from": {"userid": "bob"}},
            },
            {
                "cmd": "aibot_event_callback",
                "headers": {"req_id": "req-disc-1"},
                "body": {"event": {"eventtype": "disconnected_event"}},
            },
        ])

        class FakeConnectCtx:
            def __init__(self, conn: FakeWebSocket) -> None:
                self.conn = conn

            async def __aenter__(self) -> FakeWebSocket:
                return self.conn

            async def __aexit__(self, *_args) -> None:
                return None

        orig_status = gateway.on_status

        def track_status(state: str) -> None:
            if orig_status:
                orig_status(state)
            if state == "reconnecting":
                stop.set()

        gateway.on_status = track_status

        async def driver() -> None:
            with patch("websockets.connect", side_effect=lambda *_a, **_kw: FakeConnectCtx(ws_conn)):
                task = asyncio.create_task(gateway.run_forever())
                deadline = time.monotonic() + 3.0
                while not stop.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                self.assertTrue(stop.is_set(), "gateway did not trigger reconnect after disconnected_event")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(driver())

        sent_cmds = [item.get("cmd") for item in ws_conn.sent]
        self.assertEqual(sent_cmds[0], "aibot_subscribe")
        self.assertEqual(ws_conn.sent[0]["body"], {"bot_id": "wb-gw", "secret": "sec-gw"})
        self.assertIn("ping", sent_cmds)
        self.assertTrue(svc.processed.is_set())
        self.assertIn(("aibot_msg_callback", "wecom:wb-gw:dm:m-gw-1"), seen_events)
        self.assertIn(("aibot_event_callback", "enter_chat"), seen_events)
        self.assertIn("connected", statuses)
        self.assertIn("reconnecting", statuses)


class WeComAdminObservabilityAndSchedulerTests(unittest.TestCase):
    def test_admin_api_observability_and_scheduled_resume_for_wecom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plane = ControlPlane.open(root / "control.sqlite")
            try:
                plane.set_setting(
                    "wecom_gateway",
                    {"state": "connected", "updated_at": datetime.now(timezone.utc).isoformat()},
                )
                plane.set_setting("qq_gateway", {})

                ev_grp = Event(
                    "wecom:wb-1:group:m1",
                    "wecom.group",
                    "wr-ops",
                    "wr-ops",
                    "ops-user-1",
                    "2026-11-21T01:00:00Z",
                    "巡检今天账单",
                    [],
                    ["dsh.task"],
                    external_actor_id="ops-user-1",
                )
                ev_dm = Event(
                    "wecom:wb-1:dm:m2",
                    "wecom.dm",
                    "ops-user-2",
                    "ops-user-2",
                    "ops-user-2",
                    "2026-11-21T01:05:00Z",
                    "私聊测试",
                    [],
                    ["dsh.task"],
                    external_actor_id="ops-user-2",
                )
                plane.ingest_event(ev_grp)
                plane.ingest_event(ev_dm)
                plane.create_conversation(
                    conversation_id="wr-ops",
                    kind="wecom.group",
                    participant_refs=["ops-user-1"],
                    policy_version="test-policy",
                )

                # Observability labels
                convs = list_conversations(plane.store)
                labels = {c["conversation_id"]: c["label"] for c in convs}
                self.assertTrue(labels["wr-ops"].startswith("企微群 · "))
                self.assertTrue(labels["ops-user-2"].startswith("企微私聊 · "))

                # Admin API /api/status & /api/users & PUT /api/groups
                server = HTTPServer(
                    ("127.0.0.1", 0),
                    make_handler(plane, root, "tok-wecom", qq_app_id="", wecom_bot_id="wb-live"),
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    headers = {"Authorization": "Bearer tok-wecom"}
                    status = json.loads(
                        urlopen(Request(f"http://127.0.0.1:{server.server_port}/api/status", headers=headers)).read()
                    )
                    self.assertEqual(status["wecom_gateway"], "connected")
                    self.assertEqual(status["qq_gateway"], "disabled")
                    self.assertEqual(status["wecom_bot_id"], "wb-live")
                    self.assertIn("wecom", status["channels"])
                    self.assertEqual(status["channels"]["wecom"]["state"], "connected")

                    users_payload = json.loads(
                        urlopen(Request(f"http://127.0.0.1:{server.server_port}/api/users", headers=headers)).read()
                    )
                    seen = {(a["provider"], a["external_id"]) for a in users_payload["seen_external_actors"]}
                    self.assertIn(("wecom", "ops-user-1"), seen)
                    self.assertIn(("wecom", "ops-user-2"), seen)

                    grp_req = Request(
                        f"http://127.0.0.1:{server.server_port}/api/groups",
                        data=json.dumps({
                            "source_type": "wecom.group",
                            "source_id": "wr-ops",
                            "group_name": "运维群",
                            "tier": "internal",
                        }).encode(),
                        method="PUT",
                        headers={**headers, "Content-Type": "application/json"},
                    )
                    grp_resp = json.loads(urlopen(grp_req).read())
                    self.assertEqual(grp_resp["group"]["tier"], "internal")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

                # ScheduledResumeHandler routes wecom schedule destination to wecom channel + outbox
                api = WeComStubAPI()
                wf = WeComStubWorkflow()
                wf.publication = {
                    "recommendation": "group_update",
                    "body": "定时汇报完成",
                    "reason": "scheduled wecom update",
                }
                wf.run_data = {"status": "observed", "work_item_id": None}
                wecom_svc = WeComChannelService(plane, wf, api, WeComBotSettings("wb-1", "sec-1", allowed_publications=frozenset({"group_update"})))
                qq_stub = SimpleNamespace(plane=plane, workflow=WeComStubWorkflow(), channel_name="qq")
                handler = ScheduledResumeHandler({"qq": qq_stub, "wecom": wecom_svc})
                task = ScheduledTask(
                    "sched-wc-1",
                    "running",
                    "2026-11-21T02:00:00Z",
                    {
                        "source_event_id": ev_grp.event_id,
                        "work_item_id": "w-wc",
                        "resume_prompt": "汇总每日数据",
                        "context": {"source_event": ev_grp.to_dict(), "summary": "昨日数据已拉取"},
                        "destination": {"channel": "wecom", "chatid": "wr-ops", "chat_type": "2"},
                    },
                    "",
                    "",
                )
                handler(task)
                self.assertEqual(len(wf.events), 1)
                self.assertEqual(wf.events[0].source_type, "wecom.group")
                self.assertTrue(any(m.destination.get("channel") == "wecom" and "定时汇报完成" in m.body for m in api.text_messages))

                # Also verify disconnected WeComBotAPI (as in admin-serve) leaves Outbox pending instead of burning attempts
                offline_api = WeComBotAPI(WeComBotSettings("wb-1", "sec-1"))
                offline_svc = WeComChannelService(
                    plane, wf, offline_api,
                    WeComBotSettings("wb-1", "sec-1", allowed_publications=frozenset({"group_update"})),
                )
                task2 = ScheduledTask(
                    "sched-wc-2",
                    "running",
                    "2026-11-21T02:05:00Z",
                    {
                        "source_event_id": ev_grp.event_id,
                        "work_item_id": "w-wc-2",
                        "resume_prompt": "二次巡检",
                        "context": {"source_event": ev_grp.to_dict(), "summary": "上下文"},
                        "destination": {"channel": "wecom", "chatid": "wr-ops", "chat_type": "2"},
                    },
                    "",
                    "",
                )
                ScheduledResumeHandler({"qq": qq_stub, "wecom": offline_svc})(task2)
                pending = [m for m in plane.list_outbox() if m.status == "pending" and m.destination.get("channel") == "wecom"]
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].attempts, 0)
            finally:
                plane.close()

    def test_concurrent_same_group_requests_queue_cleanly_and_timeout_clears_queued_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plane = ControlPlane.open(root / "control.sqlite")
            manager = WorkspaceManager(root / "workspaces")
            try:
                first_entered = threading.Event()
                release_first = threading.Event()
                dispatch_order: list[str] = []

                class Upstream:
                    def submit_event(self, event: Event, *, context=None):
                        return DssuperAgentReceipt("stored", f"trig:{event.event_id}", f"thr:{event.conversation_id}")

                    def wait_for_decision(self, _thread_key: str, trigger_id: str, *, timeout: float = 600):
                        eid = trigger_id.removeprefix("trig:")
                        label = "第二题营收" if "msg-2" in eid else "第一题排行"
                        return DssuperAgentDecision(
                            decision_id=f"dec-{hashlib.sha256(eid.encode()).hexdigest()[:12]}",
                            trigger_ids=[trigger_id],
                            thread_key="thr:wr-same",
                            kind="ignore",
                            outcome="ignore",
                            reason="run dsh",
                            occurred_at="2026-11-22T05:00:00Z",
                            strength="inferred",
                            assistant_intent={
                                "action": "propose_work",
                                "rationale": label,
                                "title": label,
                                "objective": label,
                                "completion_conditions": ["完成"],
                                "plan_steps": [{
                                    "title": label,
                                    "objective": label,
                                    "capability": "dsh.task",
                                    "completion_conditions": ["完成"],
                                }],
                                "publication": {"recommendation": "group_update", "reason": "群内询问", "body": label},
                            },
                        )

                    def submit_execution_result(self, event: Event, *, context=None):
                        return DssuperAgentReceipt("stored", f"res:{event.event_id}", f"thr:{event.conversation_id}")

                class QueuingAdapter:
                    def __init__(self) -> None:
                        self.config = SimpleNamespace(
                            dsh_default_timeout_seconds=120.0,
                            dsh_idle_timeout_seconds=60.0,
                            dsh_compaction_timeout_seconds=60.0,
                            redact=lambda text: text,
                        )

                    def preflight(self, request, workspace):
                        return SimpleNamespace(
                            knowledge_document_ids=[],
                            attachment_ids=[],
                            prompt=request.parameters.get("prompt", ""),
                        )

                    def execute(self, request, workspace, task, *, lease_fd=None, workspace_tier=None):
                        exec_id = request.execution_id
                        dispatch_order.append(f"start:{exec_id}")
                        if len(dispatch_order) == 1:
                            first_entered.set()
                            release_first.wait(timeout=3.0)
                        artifact_rel = request.parameters["expected_artifacts"][0]
                        artifact_path = workspace.root / artifact_rel
                        artifact_path.parent.mkdir(parents=True, exist_ok=True)
                        first_exec_id = plane.get_assistant_run("wecom:wb-q:group:msg-1").execution_ids[0]
                        answer = "第一条结果" if exec_id == first_exec_id else "今日营收 123 元"
                        artifact_path.write_text(
                            f"<!-- COMPANY_AGENT_FINAL_REPLY_BEGIN:{exec_id} -->\n"
                            f"{answer}\n"
                            f"<!-- COMPANY_AGENT_FINAL_REPLY_END:{exec_id} -->\n",
                            encoding="utf-8",
                        )
                        dispatch_order.append(f"done:{exec_id}")
                        return ExecutionResult(exec_id, "succeeded", artifact_refs=[artifact_rel])

                dispatcher = ExecutionDispatcher(plane, manager, QueuingAdapter(), root / "runtime")
                wf = DssuperAgentWorkflow(
                    plane, Upstream(), manager=manager, dispatcher=dispatcher,
                    policy=AssistantPolicy(auto_execute_capabilities=frozenset({"dsh.task"})),
                    workspace_wait_seconds=5.0,
                )
                api = WeComStubAPI()
                svc = WeComChannelService(
                    plane, wf, api,
                    WeComBotSettings("wb-q", "sec-q",
                                     allowed_publications=frozenset({"group_update"}),
                                     auto_execute_capabilities=frozenset({"dsh.task"}),
                                     workspace_id="wecom-files"),
                    workspace_manager=manager,
                )

                t1 = threading.Thread(target=svc.handle_gateway_frame, args=({
                    "cmd": "aibot_msg_callback",
                    "headers": {"req_id": "req-q1"},
                    "body": {"msgid": "msg-1", "chattype": "group", "chatid": "wr-same",
                             "from": {"userid": "u1"}, "msgtype": "text", "text": {"content": "@Bot 问题1"}},
                },))
                t1.start()
                self.assertTrue(first_entered.wait(timeout=3.0))

                # Second message arrives concurrently while first holds the workspace lock!
                t2 = threading.Thread(target=svc.handle_gateway_frame, args=({
                    "cmd": "aibot_msg_callback",
                    "headers": {"req_id": "req-q2"},
                    "body": {"msgid": "msg-2", "chattype": "group", "chatid": "wr-same",
                             "from": {"userid": "u1"}, "msgtype": "text", "text": {"content": "@Bot 问题2"}},
                },))
                t2.start()
                time.sleep(0.15)
                release_first.set()
                t1.join(timeout=5.0)
                t2.join(timeout=5.0)

                # Both tasks completed sequentially with zero queued executions left behind!
                self.assertEqual(len(dispatch_order), 4)
                self.assertEqual(dispatch_order[0].split(":")[1], dispatch_order[1].split(":")[1])
                self.assertEqual(dispatch_order[2].split(":")[1], dispatch_order[3].split(":")[1])
                self.assertEqual(
                    plane.store.fetchall("SELECT execution_id FROM executions WHERE status IN ('queued','running')"),
                    [],
                )
                bodies = [m.body for m in api.text_messages]
                self.assertTrue(any("第一条结果" in b for b in bodies), bodies)
                self.assertTrue(any("今日营收 123 元" in b for b in bodies), bodies)

                # Now simulate a wait timeout when an external lease never releases: execution & work_item settle to failed!
                ws_id = plane.get_assistant_run("wecom:wb-q:group:msg-1").workspace_id
                wf_short = DssuperAgentWorkflow(
                    plane, Upstream(), manager=manager, dispatcher=dispatcher,
                    policy=AssistantPolicy(auto_execute_capabilities=frozenset({"dsh.task"})),
                    workspace_wait_seconds=0.05,
                )
                svc_short = WeComChannelService(
                    plane, wf_short, api,
                    WeComBotSettings("wb-q", "sec-q",
                                     allowed_publications=frozenset({"group_update"}),
                                     auto_execute_capabilities=frozenset({"dsh.task"}),
                                     workspace_id="wecom-files"),
                    workspace_manager=manager,
                )
                with manager.execution_lease(ws_id):
                    svc_short.handle_gateway_frame({
                        "cmd": "aibot_msg_callback",
                        "headers": {"req_id": "req-q3"},
                        "body": {"msgid": "msg-3", "chattype": "group", "chatid": "wr-same",
                                 "from": {"userid": "u1"}, "msgtype": "text", "text": {"content": "@Bot 超时测试"}},
                    })
                run3 = plane.get_assistant_run("wecom:wb-q:group:msg-3")
                self.assertEqual(run3.status, "failed")
                self.assertEqual(run3.error_category, "workspace_busy")
                self.assertEqual(plane.get_work_item(run3.work_item_id).status, "failed")
                self.assertEqual(plane.get_execution(run3.execution_ids[0]).status, "failed")
                self.assertEqual(
                    plane.store.fetchall("SELECT execution_id FROM executions WHERE status IN ('queued','running')"),
                    [],
                )
                svc.close()
                svc_short.close()
            finally:
                plane.close()


if __name__ == "__main__":
    unittest.main()
