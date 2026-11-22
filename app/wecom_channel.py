from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .assistant_workflow import DssuperAgentWorkflow
from .attachments import AttachmentIngestor
from .channel_core import BaseChannelService
from .control_plane import ControlPlane
from .identity import IdentityRegistry
from .knowledge.service import KnowledgeService
from .models import Event, OutboxMessage
from .runtime_config import WeComBotSettings
from .tool_forge import ForgeSecretsVault, ToolForgeService

WECOM_CMD_SUBSCRIBE = "aibot_subscribe"
WECOM_CMD_HEARTBEAT = "ping"
WECOM_CMD_MSG_CALLBACK = "aibot_msg_callback"
WECOM_CMD_EVENT_CALLBACK = "aibot_event_callback"
WECOM_CMD_RESPOND_MSG = "aibot_respond_msg"
WECOM_CMD_RESPOND_WELCOME = "aibot_respond_welcome_msg"
WECOM_CMD_SEND_MSG = "aibot_send_msg"
WECOM_CMD_UPLOAD_INIT = "aibot_upload_media_init"
WECOM_CMD_UPLOAD_CHUNK = "aibot_upload_media_chunk"
WECOM_CMD_UPLOAD_FINISH = "aibot_upload_media_finish"

WECOM_HEARTBEAT_INTERVAL_SECONDS = 30.0
WECOM_RPC_TIMEOUT_SECONDS = 15.0
WECOM_PASSIVE_WINDOW_SECONDS = 540.0
WECOM_MAX_REPLY_BYTES = 18_000
WECOM_UPLOAD_CHUNK_BYTES = 512 * 1024
WECOM_MAX_UPLOAD_CHUNKS = 100
WECOM_MAX_UPLOAD_BYTES = WECOM_UPLOAD_CHUNK_BYTES * WECOM_MAX_UPLOAD_CHUNKS
WECOM_IMAGE_MAX_BYTES = 10 * 1024 * 1024

DEFAULT_WELCOME_MESSAGE = (
    "你好，我是企业 AI 助手。你可以直接在对话中提问、发送文件或图片让我协助处理；"
    "发送 `/whoami` 可查看当前身份与权限层级，发送 `/bind <绑定码>` 可关联内部成员身份。"
)


def generate_wecom_req_id(prefix: str = "req") -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{os.urandom(4).hex()}"


def generate_wecom_stream_id() -> str:
    return f"stream_{int(time.time() * 1000)}_{os.urandom(4).hex()}"


def _wecom_media_type(file_path: Path, size: int) -> str:
    suffix = file_path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"} and size <= WECOM_IMAGE_MAX_BYTES:
        return "image"
    return "file"


def _strip_leading_bot_mention_for_command(text: str) -> str:
    """Strip leading @Bot mentions when the user immediately invokes a slash command in a WeCom group."""
    return re.sub(r"^(?:@[^\s\u2005]+[\s\u2005]+)+(?=/[A-Za-z][A-Za-z0-9_-]*)", "", text)


def _format_wecom_timestamp(raw_time: Any) -> str:
    if isinstance(raw_time, (int, float)) and math.isfinite(raw_time):
        return datetime.fromtimestamp(float(raw_time), tz=timezone.utc).isoformat()
    if isinstance(raw_time, str) and raw_time.strip():
        text = raw_time.strip()
        if text.isdigit():
            return datetime.fromtimestamp(float(text), tz=timezone.utc).isoformat()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("WeCom message timestamp is invalid") from error
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return datetime.now(timezone.utc).isoformat()


def _append_wecom_media_attachment(
    attachments: list[dict[str, str]],
    media: Any,
    *,
    default_name: str,
    default_content_type: str,
) -> None:
    if len(attachments) >= 10 or not isinstance(media, dict):
        return
    url = media.get("url")
    if not isinstance(url, str) or not url.strip():
        return
    clean_url = url.strip()
    if len(clean_url) > 2048:
        raise ValueError("WeCom attachment URL exceeds its size limit")
    raw_name = media.get("filename") or media.get("name") or default_name
    clean_name = str(raw_name).strip()[:200] or default_name
    entry: dict[str, str] = {
        "filename": clean_name,
        "content_type": default_content_type,
        "url": clean_url,
    }
    aeskey = media.get("aeskey")
    if isinstance(aeskey, str) and aeskey.strip():
        entry["decrypt_key"] = aeskey.strip()
    attachments.append(entry)


def _extract_quote_text(quote: Any, attachments: list[dict[str, str]]) -> str:
    if not isinstance(quote, dict):
        return ""
    qtype = str(quote.get("msgtype") or "").strip().lower()
    if qtype == "text":
        content = ((quote.get("text") or {}).get("content") or "").strip()
        return f"[引用消息] {content}" if content else ""
    if qtype == "voice":
        content = ((quote.get("voice") or {}).get("content") or "").strip()
        return f"[引用语音] {content}" if content else ""
    if qtype == "image":
        _append_wecom_media_attachment(
            attachments, quote.get("image"), default_name=f"wecom-quote-image-{len(attachments) + 1}",
            default_content_type="image/jpeg",
        )
        return "[引用图片]"
    if qtype == "file":
        _append_wecom_media_attachment(
            attachments, quote.get("file"), default_name=f"wecom-quote-file-{len(attachments) + 1}",
            default_content_type="application/octet-stream",
        )
        return "[引用文件]"
    if qtype == "mixed":
        parts: list[str] = []
        items = (quote.get("mixed") or {}).get("msg_item") or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("msgtype") == "text":
                    piece = str(((item.get("text") or {}).get("content")) or "").strip()
                    if piece:
                        parts.append(piece)
                elif item.get("msgtype") == "image":
                    _append_wecom_media_attachment(
                        attachments, item.get("image"),
                        default_name=f"wecom-quote-image-{len(attachments) + 1}",
                        default_content_type="image/jpeg",
                    )
        joined = "\n".join(parts).strip()
        return f"[引用消息] {joined}" if joined else ("[引用图片]" if attachments else "")
    return ""


def normalize_wecom_message(bot_id: str, frame: dict[str, Any]) -> tuple[Event, dict[str, str]] | None:
    """Normalize a WeCom AI-Bot WebSocket callback frame into a Company Agent Event + Outbox destination."""
    if not isinstance(frame, dict):
        raise ValueError("WeCom frame must be an object")
    cmd = frame.get("cmd")
    if cmd is not None and cmd != WECOM_CMD_MSG_CALLBACK:
        return None
    headers = frame.get("headers") if isinstance(frame.get("headers"), dict) else {}
    req_id = headers.get("req_id")
    body = frame.get("body") if "body" in frame else frame
    if not isinstance(body, dict):
        raise ValueError("WeCom callback body must be an object")
    msgtype = str(body.get("msgtype") or "").strip().lower()
    if msgtype == "event":
        return None
    msgid = body.get("msgid")
    chattype = body.get("chattype")
    sender = body.get("from")
    if not isinstance(sender, dict):
        raise ValueError("WeCom message is missing its sender")
    userid = sender.get("userid")
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise ValueError("WeCom bot_id is required")
    if not isinstance(msgid, str) or not msgid.strip():
        raise ValueError("WeCom message is missing msgid")
    if chattype not in {"single", "group"}:
        raise ValueError("WeCom message chattype must be single or group")
    if not isinstance(userid, str) or not userid.strip():
        raise ValueError("WeCom message is missing from.userid")
    userid = userid.strip()
    if userid == bot_id.strip():
        return None
    is_direct = chattype == "single"
    if is_direct:
        conversation_id = userid
    else:
        raw_chatid = body.get("chatid")
        if not isinstance(raw_chatid, str) or not raw_chatid.strip():
            raise ValueError("WeCom group message is missing chatid")
        conversation_id = raw_chatid.strip()

    occurred_at = _format_wecom_timestamp(body.get("create_time"))
    attachments: list[dict[str, str]] = []
    text_parts: list[str] = []

    if msgtype == "text":
        raw_text = ((body.get("text") or {}).get("content"))
        if not isinstance(raw_text, str):
            raise ValueError("WeCom text message content must be a string")
        cleaned = _strip_leading_bot_mention_for_command(raw_text.strip())
        if cleaned:
            text_parts.append(cleaned)
    elif msgtype == "voice":
        raw_voice = ((body.get("voice") or {}).get("content"))
        if not isinstance(raw_voice, str):
            raise ValueError("WeCom voice message content must be a string")
        cleaned = _strip_leading_bot_mention_for_command(raw_voice.strip())
        if cleaned:
            text_parts.append(cleaned)
    elif msgtype == "image":
        _append_wecom_media_attachment(
            attachments, body.get("image"), default_name="wecom-image-1",
            default_content_type="image/jpeg",
        )
    elif msgtype == "file":
        _append_wecom_media_attachment(
            attachments, body.get("file"), default_name="wecom-file-1",
            default_content_type="application/octet-stream",
        )
    elif msgtype == "video":
        _append_wecom_media_attachment(
            attachments, body.get("video"), default_name="wecom-video-1.mp4",
            default_content_type="video/mp4",
        )
    elif msgtype == "mixed":
        items = (body.get("mixed") or {}).get("msg_item")
        if not isinstance(items, list):
            raise ValueError("WeCom mixed message must contain msg_item list")
        for item in items:
            if not isinstance(item, dict):
                continue
            itype = str(item.get("msgtype") or "").strip().lower()
            if itype == "text":
                piece = str(((item.get("text") or {}).get("content")) or "").strip()
                if piece:
                    text_parts.append(_strip_leading_bot_mention_for_command(piece))
            elif itype == "image":
                _append_wecom_media_attachment(
                    attachments, item.get("image"),
                    default_name=f"wecom-image-{len(attachments) + 1}",
                    default_content_type="image/jpeg",
                )
    else:
        raise ValueError(f"unsupported WeCom message type: {msgtype or 'unknown'}")

    quote_prefix = _extract_quote_text(body.get("quote"), attachments)
    if quote_prefix:
        text_parts.insert(0, quote_prefix)

    content = "\n".join(part for part in text_parts if part).strip()
    if not content:
        content = "[附件]" if attachments else "[空消息]"

    event = Event(
        event_id=f"wecom:{bot_id.strip()}:{'dm' if is_direct else 'group'}:{msgid.strip()}",
        source_type="wecom.dm" if is_direct else "wecom.group",
        source_id=conversation_id,
        conversation_id=conversation_id,
        actor_id=userid,
        occurred_at=occurred_at,
        content=content[:12000],
        attachments=attachments,
        capabilities=["dsh.task"],
    )
    destination: dict[str, str] = {
        "channel": "wecom",
        "chat_type": "1" if is_direct else "2",
        "chatid": conversation_id,
        "userid": userid,
    }
    if isinstance(req_id, str) and req_id.strip():
        destination["req_id"] = req_id.strip()
        destination["received_at_epoch"] = str(time.time())
    if not is_direct and "@" in str(((body.get("text") or {}).get("content") or "")):
        destination["mentions_bot"] = "1"
    return event, destination


class WeComBotAPI:
    """Thread-safe RPC bridge over the active WeCom WebSocket long connection."""

    def __init__(self, settings: WeComBotSettings, *, timeout: float = WECOM_RPC_TIMEOUT_SECONDS) -> None:
        self.settings = settings
        self.timeout = timeout
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sender: Callable[[str, str, dict[str, Any], float], asyncio.Future[dict[str, Any]]] | None = None
        self._responded_req_ids: OrderedDict[str, float] = OrderedDict()

    def attach_transport(
        self,
        loop: asyncio.AbstractEventLoop,
        sender: Callable[[str, str, dict[str, Any], float], Any],
    ) -> None:
        with self._lock:
            self._loop = loop
            self._sender = sender

    def detach_transport(self) -> None:
        with self._lock:
            self._loop = None
            self._sender = None

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._loop is not None and self._sender is not None

    def _call_ws(self, cmd: str, req_id: str, body: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            loop = self._loop
            sender = self._sender
        if loop is None or sender is None:
            raise RuntimeError("WeCom WebSocket connection is not active")
        effective_timeout = self.timeout if timeout is None else timeout
        fut = asyncio.run_coroutine_threadsafe(sender(cmd, req_id, body, effective_timeout), loop)
        result = fut.result(timeout=effective_timeout + 2.0)
        if not isinstance(result, dict):
            raise RuntimeError("WeCom gateway returned an invalid RPC response")
        errcode = result.get("errcode", 0)
        if isinstance(errcode, int) and errcode != 0:
            raise RuntimeError(f"WeCom API error {errcode}: {result.get('errmsg', 'unknown error')}")
        return result

    def _claim_passive_req_id(self, destination: dict[str, str]) -> str | None:
        req_id = destination.get("req_id")
        if not req_id:
            return None
        received_raw = destination.get("received_at_epoch")
        if received_raw:
            try:
                if (time.time() - float(received_raw)) >= WECOM_PASSIVE_WINDOW_SECONDS:
                    return None
            except ValueError:
                return None
        with self._lock:
            if req_id in self._responded_req_ids:
                return None
            self._responded_req_ids[req_id] = time.time()
            while len(self._responded_req_ids) > 2048:
                self._responded_req_ids.popitem(last=False)
        return req_id

    def send_text_message(self, message: OutboxMessage) -> None:
        destination = message.destination
        chatid = destination.get("chatid")
        if destination.get("channel") != "wecom" or not chatid:
            raise ValueError("Outbox message does not have a WeCom destination")
        content = message.body
        raw_bytes = content.encode("utf-8")
        if len(raw_bytes) > WECOM_MAX_REPLY_BYTES:
            content = raw_bytes[:WECOM_MAX_REPLY_BYTES].decode("utf-8", errors="ignore")

        passive_req = self._claim_passive_req_id(destination)
        if passive_req:
            try:
                self._call_ws(
                    WECOM_CMD_RESPOND_MSG,
                    passive_req,
                    {
                        "msgtype": "stream",
                        "stream": {
                            "id": generate_wecom_stream_id(),
                            "finish": True,
                            "content": content,
                        },
                    },
                )
                return
            except Exception as error:
                if "not active" in str(error).lower():
                    raise
                # If WeCom rejected/expired passive req_id (e.g. stream timeout or replay), fall back to active send.
        self._send_active_payload(chatid, {"msgtype": "markdown", "markdown": {"content": content}}, destination)

    def _send_active_payload(
        self,
        chatid: str,
        payload: dict[str, Any],
        destination: dict[str, str],
    ) -> None:
        body: dict[str, Any] = {"chatid": chatid, **payload}
        chat_type_raw = destination.get("chat_type")
        if chat_type_raw in {"1", "2"}:
            body["chat_type"] = int(chat_type_raw)
        self._call_ws(WECOM_CMD_SEND_MSG, generate_wecom_req_id(WECOM_CMD_SEND_MSG), body)

    def upload_media(self, *, file_path: Path, filename: str | None = None) -> tuple[str, str]:
        """Upload a local workspace file over WeCom WebSocket chunk upload and return (media_type, media_id)."""
        resolved = Path(file_path).resolve(strict=True)
        if not resolved.is_file():
            raise FileNotFoundError(f"WeCom upload file not found: {file_path}")
        raw = resolved.read_bytes()
        size = len(raw)
        if size <= 0 or size > WECOM_MAX_UPLOAD_BYTES:
            raise ValueError(f"WeCom upload file size must be between 1 and {WECOM_MAX_UPLOAD_BYTES} bytes")
        clean_name = (filename or resolved.name).strip()[:200] or resolved.name
        media_type = _wecom_media_type(Path(clean_name), size)
        total_chunks = math.ceil(size / WECOM_UPLOAD_CHUNK_BYTES)
        if total_chunks > WECOM_MAX_UPLOAD_CHUNKS:
            raise ValueError("WeCom upload file exceeds 100 chunks limit")
        file_md5 = hashlib.md5(raw).hexdigest()
        upload_timeout = max(self.timeout, 30.0)

        init_resp = self._call_ws(
            WECOM_CMD_UPLOAD_INIT,
            generate_wecom_req_id(WECOM_CMD_UPLOAD_INIT),
            {
                "type": media_type,
                "filename": clean_name,
                "total_size": size,
                "total_chunks": total_chunks,
                "md5": file_md5,
            },
            timeout=upload_timeout,
        )
        upload_id = (init_resp.get("body") or {}).get("upload_id")
        if not isinstance(upload_id, str) or not upload_id:
            raise RuntimeError("WeCom upload_init response did not contain upload_id")

        for chunk_index in range(total_chunks):
            start = chunk_index * WECOM_UPLOAD_CHUNK_BYTES
            chunk = raw[start: start + WECOM_UPLOAD_CHUNK_BYTES]
            b64 = base64.b64encode(chunk).decode("ascii")
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    self._call_ws(
                        WECOM_CMD_UPLOAD_CHUNK,
                        generate_wecom_req_id(WECOM_CMD_UPLOAD_CHUNK),
                        {
                            "upload_id": upload_id,
                            "chunk_index": chunk_index,
                            "base64_data": b64,
                        },
                        timeout=upload_timeout,
                    )
                    last_error = None
                    break
                except Exception as error:
                    last_error = error
                    if attempt < 2:
                        time.sleep(0.3 * (attempt + 1))
            if last_error is not None:
                raise last_error

        finish_resp = self._call_ws(
            WECOM_CMD_UPLOAD_FINISH,
            generate_wecom_req_id(WECOM_CMD_UPLOAD_FINISH),
            {"upload_id": upload_id},
            timeout=upload_timeout,
        )
        media_id = (finish_resp.get("body") or {}).get("media_id")
        if not isinstance(media_id, str) or not media_id:
            raise RuntimeError("WeCom upload_finish response did not contain media_id")
        resolved_type = str((finish_resp.get("body") or {}).get("type") or media_type)
        return resolved_type, media_id

    def send_media_message(self, message: OutboxMessage, *, file_path: Path, filename: str | None = None) -> None:
        destination = message.destination
        chatid = destination.get("chatid")
        if destination.get("channel") != "wecom" or not chatid:
            raise ValueError("Outbox message does not have a WeCom destination")
        media_type, media_id = self.upload_media(file_path=file_path, filename=filename)
        self._send_active_payload(
            chatid,
            {"msgtype": media_type, media_type: {"media_id": media_id}},
            destination,
        )

    def send_welcome_reply(self, req_id: str, content: str = DEFAULT_WELCOME_MESSAGE) -> None:
        self._call_ws(
            WECOM_CMD_RESPOND_WELCOME,
            req_id,
            {"msgtype": "text", "text": {"content": content}},
            timeout=4.5,
        )


class WeComChannelService(BaseChannelService):
    channel_name = "wecom"
    provider_name = "wecom"
    channel_display_name = "企业微信"
    policy_prefix = "wecom"
    max_reply_bytes = WECOM_MAX_REPLY_BYTES

    def __init__(
        self,
        plane: ControlPlane,
        workflow: DssuperAgentWorkflow,
        api: WeComBotAPI,
        settings: WeComBotSettings,
        *,
        identities: IdentityRegistry | None = None,
        attachment_ingestor: AttachmentIngestor | None = None,
        knowledge_service: KnowledgeService | None = None,
        workspace_manager: Any | None = None,
        admin_user_ids: frozenset[str] | None = None,
        tool_forge: ToolForgeService | None = None,
        forge_secrets_vault: ForgeSecretsVault | None = None,
        welcome_message: str = DEFAULT_WELCOME_MESSAGE,
    ) -> None:
        self.api = api
        self.welcome_message = welcome_message
        super().__init__(
            plane,
            workflow,
            settings,
            identities=identities,
            attachment_ingestor=attachment_ingestor,
            knowledge_service=knowledge_service,
            workspace_manager=workspace_manager,
            admin_user_ids=admin_user_ids,
            tool_forge=tool_forge,
            forge_secrets_vault=forge_secrets_vault,
        )

    def handle_gateway_frame(self, frame: dict[str, Any]) -> None:
        cmd = frame.get("cmd")
        if cmd == WECOM_CMD_EVENT_CALLBACK:
            self.handle_event_callback(frame)
            return
        prepared = self.prepare_gateway_frame(frame)
        if prepared is None:
            return
        event, destination = prepared
        # Download & AES-decrypt immediately because WeCom media URLs expire in ~5 minutes.
        event = self.attachment_ingestor.prepare_originals(event, on_update=self.plane.update_event)
        self.process_prepared_event(event, destination)
        self.process_prepared_attachments(event)

    def prepare_gateway_frame(self, frame: dict[str, Any]) -> tuple[Event, dict[str, str]] | None:
        try:
            normalized = normalize_wecom_message(self.settings.bot_id, frame)
        except ValueError as error:
            print(f"company-agent: discarded malformed WeCom event ({type(error).__name__})", file=sys.stderr)
            return None
        if normalized is None:
            return None
        event, destination = normalized
        return self._persist_prepared_event(event, destination)

    def handle_event_callback(self, frame: dict[str, Any]) -> None:
        headers = frame.get("headers") if isinstance(frame.get("headers"), dict) else {}
        req_id = str(headers.get("req_id") or "").strip()
        body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
        ev = body.get("event") if isinstance(body.get("event"), dict) else {}
        event_type = str(ev.get("eventtype") or "").strip().lower()
        if event_type == "enter_chat" and req_id:
            try:
                self.api.send_welcome_reply(req_id, self.welcome_message)
            except Exception as error:
                print(f"company-agent: WeCom welcome reply failed ({type(error).__name__})", file=sys.stderr)

    def _event_explicitly_mentions_bot(self, event: Event, destination: dict[str, str]) -> bool:
        if getattr(self.settings, "group_direct_on_callback", True):
            return True
        return destination.get("mentions_bot") == "1"

    def _transport_ready(self) -> bool:
        check = getattr(self.api, "connected", None)
        if check is None:
            return True
        return bool(check()) if callable(check) else bool(check)

    def _deliver_outbox_message(
        self,
        message: OutboxMessage,
        *,
        file_path: Path | None = None,
        filename: str | None = None,
    ) -> None:
        if file_path is not None:
            self.api.send_media_message(message, file_path=file_path, filename=filename)
        else:
            self.api.send_text_message(message)


class WeComGateway:
    """Persistent WebSocket client for Enterprise WeChat Intelligent Robot long connections."""

    MAX_IN_FLIGHT_EVENTS = 64

    def __init__(
        self,
        api: WeComBotAPI,
        service: WeComChannelService,
        *,
        reconnect_delay: float = 2.0,
        heartbeat_interval: float = WECOM_HEARTBEAT_INTERVAL_SECONDS,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.api = api
        self.service = service
        self.reconnect_delay = reconnect_delay
        self.heartbeat_interval = heartbeat_interval
        self.on_status = on_status
        self._pending_acks: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._active_socket: Any = None
        self._send_lock: asyncio.Lock | None = None
        self._executor: ThreadPoolExecutor | None = None

    async def _run_worker(self, fn: Callable[..., Any], *args: Any) -> None:
        loop = asyncio.get_running_loop()
        if self._executor is not None:
            await loop.run_in_executor(self._executor, fn, *args)
            return
        await asyncio.to_thread(fn, *args)

    async def _send_rpc(self, cmd: str, req_id: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self._active_socket is None or self._send_lock is None:
            raise RuntimeError("WeCom WebSocket connection is not active")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_acks[req_id] = future
        frame: dict[str, Any] = {
            "cmd": cmd,
            "headers": {"req_id": req_id},
        }
        if body:
            frame["body"] = body
        try:
            async with self._send_lock:
                await self._active_socket.send(json.dumps(frame, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_acks.pop(req_id, None)

    def _dispatch_callback(self, frame: dict[str, Any]) -> asyncio.Task[None] | None:
        if len(self._event_tasks) >= self.MAX_IN_FLIGHT_EVENTS:
            raise RuntimeError("WeCom event processing is at capacity")
        cmd = frame.get("cmd")
        if cmd == WECOM_CMD_EVENT_CALLBACK:
            body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
            ev = body.get("event") if isinstance(body.get("event"), dict) else {}
            if ev.get("eventtype") == "disconnected_event":
                raise ConnectionError("WeCom gateway superseded by a newer connection (disconnected_event)")
            task = asyncio.create_task(self._run_worker(self.service.handle_event_callback, frame))
            self._track_task(task)
            return task

        prepared = self.service.prepare_gateway_frame(frame)
        if prepared is None:
            return None
        event, destination = prepared

        def _run_event() -> None:
            ready = self.service.attachment_ingestor.prepare_originals(
                event, on_update=self.service.plane.update_event,
            )
            self.service.process_prepared_event(ready, destination)
            self.service.process_prepared_attachments(ready)

        task = asyncio.create_task(self._run_worker(_run_event))
        self._track_task(task)
        return task

    def _track_task(self, task: asyncio.Task[None]) -> None:
        self._event_tasks.add(task)
        task.add_done_callback(self._event_task_finished)

    def _event_task_finished(self, task: asyncio.Task[None]) -> None:
        self._event_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            print(f"company-agent: WeCom event worker failed ({type(error).__name__})", file=sys.stderr)

    def _fail_pending_acks(self, reason: str) -> None:
        for req_id, future in list(self._pending_acks.items()):
            if not future.done():
                future.set_exception(ConnectionError(reason))
            self._pending_acks.pop(req_id, None)

    def _handle_incoming_frame(self, frame: dict[str, Any]) -> None:
        cmd = frame.get("cmd")
        headers = frame.get("headers") if isinstance(frame.get("headers"), dict) else {}
        req_id = str(headers.get("req_id") or "")
        if cmd in {WECOM_CMD_MSG_CALLBACK, WECOM_CMD_EVENT_CALLBACK}:
            self._dispatch_callback(frame)
            return
        if req_id and req_id in self._pending_acks:
            fut = self._pending_acks.pop(req_id)
            if not fut.done():
                fut.set_result(frame)

    async def run_forever(self) -> None:
        import websockets

        delay = self.reconnect_delay
        executor = ThreadPoolExecutor(max_workers=self.MAX_IN_FLIGHT_EVENTS, thread_name_prefix="wecom-worker")
        self._executor = executor
        try:
            while True:
                heartbeat = None
                loop = asyncio.get_running_loop()
                self._send_lock = asyncio.Lock()
                try:
                    async with websockets.connect(
                        self.api.settings.ws_url, ping_interval=None, close_timeout=5, max_size=8 * 1024 * 1024,
                    ) as socket:
                        self._active_socket = socket
                        sub_req_id = generate_wecom_req_id(WECOM_CMD_SUBSCRIBE)
                        sub_frame = {
                            "cmd": WECOM_CMD_SUBSCRIBE,
                            "headers": {"req_id": sub_req_id},
                            "body": {
                                "bot_id": self.api.settings.bot_id,
                                "secret": self.api.settings.bot_secret,
                            },
                        }
                        await socket.send(json.dumps(sub_frame))
                        raw_auth = await asyncio.wait_for(socket.recv(), timeout=15.0)
                        auth_resp = json.loads(raw_auth)
                        if not isinstance(auth_resp, dict) or auth_resp.get("errcode", -1) != 0:
                            errmsg = auth_resp.get("errmsg") if isinstance(auth_resp, dict) else "invalid auth response"
                            raise ConnectionError(f"WeCom authentication failed: {errmsg}")
                        delay = self.reconnect_delay
                        self.api.attach_transport(loop, self._send_rpc)
                        if self.on_status:
                            self.on_status("connected")
                        heartbeat = asyncio.create_task(self._heartbeat_loop())
                        try:
                            async for raw in socket:
                                frame = json.loads(raw)
                                if not isinstance(frame, dict):
                                    raise RuntimeError("WeCom gateway sent an invalid frame")
                                self._handle_incoming_frame(frame)
                        finally:
                            self.api.detach_transport()
                            self._active_socket = None
                            self._fail_pending_acks("WeCom WebSocket connection closed")
                            if heartbeat:
                                heartbeat.cancel()
                                await asyncio.gather(heartbeat, return_exceptions=True)
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.api.detach_transport()
                    self._active_socket = None
                    self._fail_pending_acks(f"WeCom WebSocket error: {type(error).__name__}")
                    if self.on_status:
                        self.on_status("reconnecting")
                    print(f"company-agent: WeCom gateway reconnecting after {type(error).__name__}: {error}", file=sys.stderr)
                    await asyncio.sleep(delay)
                    delay = min(30.0, delay * 2)
        finally:
            self._executor = None
            executor.shutdown(wait=False)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            req_id = generate_wecom_req_id(WECOM_CMD_HEARTBEAT)
            resp = await self._send_rpc(WECOM_CMD_HEARTBEAT, req_id, {}, min(10.0, self.heartbeat_interval))
            if resp.get("errcode", 0) != 0:
                raise ConnectionError(f"WeCom heartbeat rejected: {resp.get('errmsg', 'unknown error')}")
