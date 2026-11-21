from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import math
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, quote
from urllib.request import Request, urlopen

from .assistant_workflow import DssuperAgentWorkflow
from .attachments import AttachmentIngestor
from .control_plane import ControlPlane, InvalidTransition, NotFound
from .dssuperagent import DssuperAgentError
from .identity import IdentityRegistry
from .knowledge.service import KnowledgeService
from .models import Event, OutboxMessage
from .reply_contract import extract_final_reply
from .runtime_config import QQBotSettings
from .workspace_retention import resolve_workspace_file, touch_workspace_files

QQ_GROUP_AND_C2C_MESSAGE_INTENT = 1 << 25
QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
DEFAULT_BOT_ALIASES = ("憨憨", "机器人", "小助手", "AI助手", "助手")
DEFAULT_AMBIENT_COOLDOWN_SECONDS = 300.0
_QQ_DIRECT_UPLOAD_LIMIT_BYTES = 5 * 1024 * 1024
_QQ_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _qq_media_file_type(filename: str) -> int:
    suffix = Path(filename).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return 1
    if suffix == ".mp4":
        return 2
    if suffix in {".silk", ".mp3", ".wav"}:
        return 3
    return 4


def normalize_qq_message(app_id: str, event_type: str, payload: dict[str, Any]) -> tuple[Event, dict[str, str]] | None:
    if event_type not in {"GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"}:
        return None
    is_direct = event_type == "C2C_MESSAGE_CREATE"
    if not isinstance(payload, dict):
        raise ValueError("QQ message payload must be an object")
    author = payload.get("author")
    if author is None:
        raise ValueError("QQ message is missing its author")
    if not isinstance(author, dict):
        raise ValueError("QQ message author must be an object")
    if author.get("bot") is True:
        return None
    message_id = payload.get("id")
    conversation_id = author.get("user_openid") if is_direct else payload.get("group_openid")
    member_id = author.get("user_openid") if is_direct else author.get("member_openid")
    timestamp = payload.get("timestamp")
    if not all(isinstance(value, str) and value.strip()
               for value in (app_id, message_id, conversation_id, member_id, timestamp)):
        raise ValueError("QQ message is missing its id, conversation, user, or timestamp")
    try:
        occurred_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("QQ message timestamp must be ISO-8601") from error
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError("QQ message content must be a string")
    raw_attachments = payload.get("attachments", [])
    if (not isinstance(raw_attachments, list) or len(raw_attachments) > 10
            or any(not isinstance(item, dict) for item in raw_attachments)):
        raise ValueError("QQ message attachments must be an array of at most ten objects")
    attachments = [{key: value for key, value in item.items()
                    if key in {"content_type", "filename", "url"} and isinstance(value, str)}
                   for item in raw_attachments]
    if any(len(value) > (2048 if key == "url" else 200)
           for item in attachments for key, value in item.items()):
        raise ValueError("QQ attachment metadata exceeds its size limit")
    body = content.strip() or ("[附件]" if attachments else "[空消息]")
    event = Event(
        event_id=f"qq:{app_id}:{'c2c' if is_direct else 'group'}:{message_id}",
        source_type="qq.c2c" if is_direct else "qq.group",
        source_id=conversation_id,
        conversation_id=conversation_id,
        actor_id=member_id,
        occurred_at=timestamp,
        content=body,
        attachments=attachments,
        capabilities=["dsh.task"],
    )
    reply_window = 60 * 60 if is_direct else 5 * 60
    destination = {"channel": "qq", "reply_to_message_id": message_id,
                   "reply_deadline_epoch": str(occurred_at.timestamp() + reply_window)}
    destination["user_openid" if is_direct else "group_openid"] = conversation_id
    if event_type == "GROUP_AT_MESSAGE_CREATE":
        destination["mentions_bot"] = "1"
    return event, destination


def normalize_group_message(app_id: str, event_type: str, payload: dict[str, Any]) -> tuple[Event, dict[str, str]] | None:
    if event_type != "GROUP_MESSAGE_CREATE":
        return None
    return normalize_qq_message(app_id, event_type, payload)


class QQBotAPI:
    def __init__(self, settings: QQBotSettings, *, timeout: float = 15):
        self.settings = settings
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _request(self, method: str, url: str, *, body: dict[str, Any] | None = None,
                 token: str | None = None, extra_headers: dict[str, str] | None = None,
                 timeout: float | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"QQBot {token}"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout if timeout is None else timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"QQ API returned HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"QQ API request failed: {error}") from error
        if not isinstance(result, dict):
            raise RuntimeError("QQ API returned a non-object response")
        if isinstance(result.get("code"), int) and result["code"] != 0:
            raise RuntimeError(f"QQ API error {result['code']}: {result.get('message', 'unknown error')}")
        return result

    def _put_presigned_part(self, presigned_url: str, chunk: bytes) -> None:
        parsed = urlsplit(presigned_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("QQ presigned upload URL must be an HTTPS URL")
        request = Request(
            presigned_url,
            data=chunk,
            headers={"Content-Length": str(len(chunk))},
            method="PUT",
        )
        try:
            with urlopen(request, timeout=max(self.timeout, 60.0)) as response:
                if not 200 <= getattr(response, "status", 200) < 300:
                    raise RuntimeError(f"QQ chunk upload returned HTTP {response.status}")
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"QQ chunk upload returned HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"QQ chunk upload failed: {error}") from error

    def access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token
        result = self._request("POST", QQ_TOKEN_URL, body={
            "appId": self.settings.app_id,
            "clientSecret": self.settings.client_secret,
        })
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("QQ token response did not contain access_token")
        try:
            expires_in = max(60, int(result.get("expires_in", 7200)))
        except (TypeError, ValueError):
            expires_in = 7200
        self._token = token
        self._token_expires_at = time.monotonic() + expires_in
        return token

    def gateway_url(self) -> str:
        result = self._request("GET", f"{self.settings.api_base_url}/gateway/bot", token=self.access_token())
        url = result.get("url")
        if not isinstance(url, str) or not url.startswith("wss://"):
            raise RuntimeError("QQ gateway response did not contain a secure websocket URL")
        parsed = urlsplit(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("v", "1")
        query.setdefault("encoding", "json")
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))

    def send_group_message(self, message: OutboxMessage) -> None:
        destination = message.destination
        group_id = destination.get("group_openid")
        if destination.get("channel") != "qq" or not group_id:
            raise ValueError("Outbox message does not have a QQ group destination")
        self._send_qq_message(f"{self.settings.api_base_url}/v2/groups/{quote(group_id, safe='')}/messages", message)

    def send_c2c_message(self, message: OutboxMessage) -> None:
        destination = message.destination
        user_id = destination.get("user_openid")
        if destination.get("channel") != "qq" or not user_id:
            raise ValueError("Outbox message does not have a QQ C2C destination")
        self._send_qq_message(f"{self.settings.api_base_url}/v2/users/{quote(user_id, safe='')}/messages", message)

    def send_group_file(self, message: OutboxMessage, *, file_path: Path, filename: str | None = None) -> None:
        destination = message.destination
        group_id = destination.get("group_openid")
        if destination.get("channel") != "qq" or not group_id:
            raise ValueError("Outbox message does not have a QQ group destination")
        file_info = self.upload_file(target_kind="group", target_id=group_id, file_path=file_path, filename=filename)
        self._send_qq_payload(
            f"{self.settings.api_base_url}/v2/groups/{quote(group_id, safe='')}/messages",
            {"msg_type": 7, "media": {"file_info": file_info}},
            message.destination,
            extra_headers={"X-Union-Appid": self.settings.app_id},
        )

    def send_c2c_file(self, message: OutboxMessage, *, file_path: Path, filename: str | None = None) -> None:
        destination = message.destination
        user_id = destination.get("user_openid")
        if destination.get("channel") != "qq" or not user_id:
            raise ValueError("Outbox message does not have a QQ C2C destination")
        file_info = self.upload_file(target_kind="c2c", target_id=user_id, file_path=file_path, filename=filename)
        self._send_qq_payload(
            f"{self.settings.api_base_url}/v2/users/{quote(user_id, safe='')}/messages",
            {"msg_type": 7, "media": {"file_info": file_info}},
            message.destination,
            extra_headers={"X-Union-Appid": self.settings.app_id},
        )

    def upload_file(self, *, target_kind: str, target_id: str, file_path: Path,
                    filename: str | None = None) -> str:
        """Upload a local workspace file to QQ Bot media API and return file_info."""
        if target_kind not in {"group", "c2c"} or not isinstance(target_id, str) or not target_id.strip():
            raise ValueError("QQ file upload requires target_kind ('group' or 'c2c') and target_id")
        resolved = Path(file_path).resolve(strict=True)
        if not resolved.is_file():
            raise FileNotFoundError(f"QQ upload file not found: {file_path}")
        raw = resolved.read_bytes()
        size = len(raw)
        if size <= 0 or size > _QQ_MAX_UPLOAD_BYTES:
            raise ValueError(f"QQ upload file size must be between 1 and {_QQ_MAX_UPLOAD_BYTES} bytes")
        clean_name = (filename or resolved.name).strip()[:200] or resolved.name
        file_type = _qq_media_file_type(clean_name)
        route = "groups" if target_kind == "group" else "users"
        encoded_id = quote(target_id, safe="")
        base_prefix = f"{self.settings.api_base_url}/v2/{route}/{encoded_id}"
        token = self.access_token()
        headers = {"X-Union-Appid": self.settings.app_id}
        upload_timeout = max(self.timeout, 60.0)
        if size <= _QQ_DIRECT_UPLOAD_LIMIT_BYTES:
            result = self._request(
                "POST",
                f"{base_prefix}/files",
                body={
                    "file_type": file_type,
                    "file_name": clean_name,
                    "file_data": base64.b64encode(raw).decode("ascii"),
                    "srv_send_msg": False,
                },
                token=token,
                extra_headers=headers,
                timeout=upload_timeout,
            )
        else:
            file_md5 = hashlib.md5(raw).hexdigest()
            file_sha1 = hashlib.sha1(raw).hexdigest()
            head_md5 = hashlib.md5(raw[:10_002_432]).hexdigest()
            prep = self._request(
                "POST",
                f"{base_prefix}/upload_prepare",
                body={
                    "file_type": file_type,
                    "file_name": clean_name,
                    "file_size": str(size),
                    "md5": file_md5,
                    "sha1": file_sha1,
                    "md5_10m": head_md5,
                },
                token=token,
                extra_headers=headers,
                timeout=upload_timeout,
            )
            upload_id = prep.get("upload_id")
            if not isinstance(upload_id, str) or not upload_id:
                raise RuntimeError("QQ upload_prepare response did not contain upload_id")
            parts = prep.get("parts") or []
            block_size = int(prep.get("block_size") or _QQ_DIRECT_UPLOAD_LIMIT_BYTES)
            if not isinstance(parts, list) or not parts:
                raise RuntimeError("QQ upload_prepare response did not contain upload parts")
            offset = 0
            for part in parts:
                if not isinstance(part, dict) or not isinstance(part.get("presigned_url"), str):
                    raise RuntimeError("QQ upload_prepare returned an invalid part entry")
                part_index = int(part.get("index", 0))
                part_len = int(part.get("block_size") or block_size)
                chunk = raw[offset: offset + part_len]
                offset += len(chunk)
                self._put_presigned_part(part["presigned_url"], chunk)
                self._request(
                    "POST",
                    f"{base_prefix}/upload_part_finish",
                    body={
                        "upload_id": upload_id,
                        "part_index": part_index,
                        "block_size": str(len(chunk)),
                        "md5": hashlib.md5(chunk).hexdigest(),
                    },
                    token=token,
                    extra_headers=headers,
                    timeout=upload_timeout,
                )
            result = self._request(
                "POST",
                f"{base_prefix}/files",
                body={
                    "file_type": file_type,
                    "file_name": clean_name,
                    "upload_id": upload_id,
                    "srv_send_msg": False,
                },
                token=token,
                extra_headers=headers,
                timeout=upload_timeout,
            )
        file_info = result.get("file_info")
        if not isinstance(file_info, str) or not file_info:
            raise RuntimeError("QQ file upload response did not contain file_info")
        return file_info

    def _send_qq_message(self, url: str, message: OutboxMessage) -> None:
        self._send_qq_payload(url, {"msg_type": 0, "content": message.body}, message.destination)

    def _send_qq_payload(self, url: str, payload: dict[str, Any], destination: dict[str, str],
                         *, extra_headers: dict[str, str] | None = None) -> None:
        body: dict[str, Any] = dict(payload)
        _add_passive_reply_reference(body, destination)
        token = self.access_token()
        req_kwargs: dict[str, Any] = {"body": body, "token": token}
        if extra_headers:
            req_kwargs["extra_headers"] = extra_headers
        try:
            self._request("POST", url, **req_kwargs)
        except RuntimeError as error:
            if "msg_id" not in body or not _is_passive_reply_expired_error(error):
                raise
            # Clock skew or delayed queue flush may reject msg_id right near the passive deadline; retry as active send.
            retry_kwargs: dict[str, Any] = {"body": dict(payload), "token": token}
            if extra_headers:
                retry_kwargs["extra_headers"] = extra_headers
            self._request("POST", url, **retry_kwargs)


def _is_passive_reply_expired_error(error: Exception) -> bool:
    detail = str(error)
    if not detail.startswith(("QQ API returned HTTP ", "QQ API error ")):
        return False
    lowered = detail.lower()
    markers = ("msg_id", "msg_seq", "expire", "passive", "被动", "过期", "304023", "304024", "22009", "400340")
    return any(marker in lowered for marker in markers)


def _add_passive_reply_reference(body: dict[str, Any], destination: dict[str, str]) -> None:
    reply_id = destination.get("reply_to_message_id")
    if not reply_id:
        return
    deadline = destination.get("reply_deadline_epoch")
    if deadline is None:
        body["msg_id"] = reply_id
        return
    try:
        expires_at = float(deadline)
    except ValueError as error:
        raise ValueError("QQ reply deadline is invalid") from error
    if not math.isfinite(expires_at):
        raise ValueError("QQ reply deadline is invalid")
    if time.time() < expires_at:
        try:
            sequence = int(destination.get("msg_seq", "1"))
        except ValueError as error:
            raise ValueError("QQ reply sequence is invalid") from error
        if not 1 <= sequence <= 5:
            raise ValueError("QQ allows at most five passive replies to one message")
        body["msg_id"] = reply_id
        body["msg_seq"] = sequence


class QQChannelService:
    def __init__(self, plane: ControlPlane, workflow: DssuperAgentWorkflow, api: QQBotAPI,
                 settings: QQBotSettings, *, identities: IdentityRegistry | None = None,
                 attachment_ingestor: AttachmentIngestor | None = None,
                 knowledge_service: KnowledgeService | None = None,
                 workspace_manager: Any | None = None,
                 admin_user_ids: frozenset[str] | None = None):
        self.plane = plane
        self.workflow = workflow
        self.api = api
        self.settings = settings
        self.identities = identities or IdentityRegistry(plane.store)
        self.knowledge_service = knowledge_service
        self.workspace_manager = workspace_manager
        self.admin_user_ids = admin_user_ids
        self.attachment_ingestor = attachment_ingestor or AttachmentIngestor(
            plane.store, knowledge_service=knowledge_service
        )
        self._outbox_lock = threading.Lock()
        self._inflight_destinations_lock = threading.Lock()
        self._inflight_destinations: dict[str, dict[str, str]] = {}
        self._ambient_lock = threading.Lock()
        self._ambient_pending: dict[str, tuple[Event, dict[str, str], threading.Timer]] = {}
        self._ambient_inflight: set[threading.Thread] = set()
        self._closed = False
        if hasattr(workflow, "on_execution_started") and getattr(workflow, "on_execution_started", None) is None:
            workflow.on_execution_started = self._on_execution_started

    def close(self) -> None:
        with self._ambient_lock:
            self._closed = True
            for _, _, timer in self._ambient_pending.values():
                timer.cancel()
            self._ambient_pending.clear()
            inflight = [t for t in self._ambient_inflight if t is not threading.current_thread()]
        for thread in inflight:
            thread.join(timeout=15.0)

    def bot_aliases(self) -> tuple[str, ...]:
        configured = self.plane.get_setting("bot_aliases", None)
        if isinstance(configured, list):
            cleaned = tuple(str(item).strip() for item in configured if isinstance(item, str) and item.strip())
            if cleaned:
                return cleaned
        return DEFAULT_BOT_ALIASES

    def ambient_listen_enabled(self) -> bool:
        return bool(self.plane.get_setting("ambient_listen_enabled", False))

    def ambient_cooldown_seconds(self) -> float:
        raw = self.plane.get_setting("ambient_cooldown_seconds", DEFAULT_AMBIENT_COOLDOWN_SECONDS)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_AMBIENT_COOLDOWN_SECONDS
        return max(0.0, min(value, 3600.0))

    def handle_gateway_event(self, event_type: str, payload: dict[str, Any]) -> None:
        prepared = self.prepare_gateway_event(event_type, payload)
        if prepared is None:
            return
        event, destination = prepared
        event = self.attachment_ingestor.prepare_originals(event, on_update=self.plane.update_event)
        self.process_prepared_event(event, destination)
        self.process_prepared_attachments(event)

    def prepare_gateway_event(self, event_type: str, payload: dict[str, Any]) -> tuple[Event, dict[str, str]] | None:
        """Persist event and attachment placeholders synchronously in QQ receive order."""
        try:
            normalized = normalize_qq_message(self.settings.app_id, event_type, payload)
        except ValueError as error:
            print(f"company-agent: discarded malformed QQ event ({type(error).__name__})", file=sys.stderr)
            return
        if normalized is None:
            return
        event, destination = normalized
        external_actor_id = event.actor_id
        try:
            saved_event = self.plane.get_event(event.event_id)
        except NotFound:
            saved_event = None
        if saved_event is not None:
            if (saved_event.source_type != event.source_type or saved_event.source_id != event.source_id
                    or saved_event.conversation_id != event.conversation_id
                    or saved_event.occurred_at != event.occurred_at
                    or not saved_event.content.startswith(event.content)
                    or (saved_event.external_actor_id is not None
                        and saved_event.external_actor_id != external_actor_id)):
                print("company-agent: discarded conflicting duplicate QQ event", file=sys.stderr)
                return
            event = Event(saved_event.event_id, saved_event.source_type, saved_event.source_id,
                          saved_event.conversation_id, saved_event.actor_id, saved_event.occurred_at,
                          saved_event.content, saved_event.attachments, saved_event.capabilities,
                          external_actor_id=saved_event.external_actor_id)
        else:
            identity_scope = event.source_id if event.source_type == "qq.group" else "*"
            event = Event(event.event_id, event.source_type, event.source_id, event.conversation_id,
                          self.identities.resolve(provider="qq", account_id=self.settings.app_id,
                                                  scope_id=identity_scope, external_id=external_actor_id),
                          event.occurred_at, event.content, event.attachments, event.capabilities,
                          external_actor_id=external_actor_id)
        if event.source_type in {"qq.group", "qq.guild"}:
            self.identities.register_group_seen(event.source_type, event.source_id)
        event = self.attachment_ingestor.register_event(event)
        if hasattr(self.workflow, "record_event_context"):
            event = self.workflow.record_event_context(event)
        else:
            self.plane.ingest_event(event)
        return event, destination

    def process_prepared_event(self, event: Event, destination: dict[str, str]) -> None:
        if self._handle_slash_command(event, destination):
            self._cancel_ambient(event.conversation_id)
            self.flush_outbox()
            return
        if event.attachments and event.content.strip() in {"[附件]", "[空消息]"}:
            return
        route = self._classify_message(event, destination)
        if route == "direct":
            self._cancel_ambient(event.conversation_id)
            self._dispatch_prepared_event(event, destination, interaction_mode="direct")
            self.flush_outbox()
            return
        if not self.ambient_listen_enabled():
            return
        if route == "chatter":
            cooldown = self.ambient_cooldown_seconds()
            if cooldown > 0:
                self._postpone_pending_ambient(event.conversation_id, cooldown)
            return
        cooldown = self.ambient_cooldown_seconds()
        if cooldown <= 0:
            self._cancel_ambient(event.conversation_id)
            self._dispatch_prepared_event(event, destination, interaction_mode="ambient_cooldown")
            self.flush_outbox()
            return
        self._schedule_ambient(event, destination, cooldown)

    def _schedule_ambient(self, event: Event, destination: dict[str, str], cooldown: float) -> None:
        with self._ambient_lock:
            if self._closed:
                return
            previous = self._ambient_pending.get(event.conversation_id)
            if previous is not None:
                previous[2].cancel()
            timer = threading.Timer(
                cooldown,
                self._fire_ambient,
                args=(event.conversation_id, event.event_id),
            )
            timer.daemon = True
            self._ambient_pending[event.conversation_id] = (event, dict(destination), timer)
            timer.start()

    def _postpone_pending_ambient(self, conversation_id: str, cooldown: float) -> None:
        with self._ambient_lock:
            if self._closed:
                return
            previous = self._ambient_pending.get(conversation_id)
            if previous is None:
                return
            topic_event, destination, old_timer = previous
            old_timer.cancel()
            timer = threading.Timer(
                cooldown,
                self._fire_ambient,
                args=(conversation_id, topic_event.event_id),
            )
            timer.daemon = True
            self._ambient_pending[conversation_id] = (topic_event, destination, timer)
            timer.start()

    def _cancel_ambient(self, conversation_id: str) -> None:
        with self._ambient_lock:
            pending = self._ambient_pending.pop(conversation_id, None)
            if pending is not None:
                pending[2].cancel()

    def _fire_ambient(self, conversation_id: str, expected_event_id: str) -> None:
        current = threading.current_thread()
        with self._ambient_lock:
            if self._closed:
                return
            pending = self._ambient_pending.get(conversation_id)
            if pending is None or pending[0].event_id != expected_event_id:
                return
            event, destination, _ = self._ambient_pending.pop(conversation_id)
            self._ambient_inflight.add(current)
        try:
            self._dispatch_prepared_event(event, destination, interaction_mode="ambient_cooldown")
            self.flush_outbox()
        finally:
            with self._ambient_lock:
                self._ambient_inflight.discard(current)

    def _dispatch_prepared_event(self, event: Event, destination: dict[str, str], *, interaction_mode: str) -> None:
        with self._inflight_destinations_lock:
            self._inflight_destinations[event.event_id] = destination
        try:
            workspace_id = self._workspace_for_event(event)
            fn = self.workflow.process_event
            params = inspect.signature(fn).parameters
            kwargs: dict[str, Any] = {}
            accepts_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            if self.settings.auto_execute_capabilities and workspace_id:
                kwargs["workspace_id"] = workspace_id
            if accepts_varkw or "interaction_mode" in params:
                kwargs["interaction_mode"] = interaction_mode
            run = fn(event, **kwargs)
            run_data = run.to_dict()
            if run_data.get("status") == "failed":
                if interaction_mode != "ambient_cooldown":
                    self._publish_processing_failure(event, destination, run_data.get("error_category"))
            else:
                self._decide_publication(event, run_data, destination)
        except Exception as error:
            if interaction_mode != "ambient_cooldown":
                self._publish_processing_failure(event, destination, getattr(error, "category", type(error).__name__))
            print(f"company-agent: QQ event processing failed ({type(error).__name__})", file=sys.stderr)
        finally:
            with self._inflight_destinations_lock:
                self._inflight_destinations.pop(event.event_id, None)

    def _actor_display_name(self, actor_id: str) -> str:
        user = self.identities.get_user(actor_id)
        if user is not None and user.display_name.strip():
            return user.display_name.strip()[:80]
        return actor_id

    def _recent_classify_context(self, event: Event, *, limit: int = 15) -> list[dict[str, Any]]:
        rows = self.plane.store.fetchall(
            "SELECT event_id, payload FROM events WHERE event_id != ? ORDER BY created_at DESC, rowid DESC LIMIT 40",
            (event.event_id,),
        )
        matched: list[dict[str, Any]] = []
        for row in rows:
            try:
                prev = self.plane.store.decode(row["payload"])
            except (TypeError, ValueError):
                continue
            if (prev.get("conversation_id") != event.conversation_id
                    or prev.get("source_type") != event.source_type):
                continue
            matched.append(prev)
            if len(matched) >= limit:
                break
        matched.reverse()
        items: list[dict[str, Any]] = []
        for prev in matched:
            att_names = [
                str(att.get("filename") or "attachment")[:255]
                for att in (prev.get("attachments") or [])[:10]
                if isinstance(att, dict)
            ]
            user_entry: dict[str, Any] = {
                "role": "user",
                "actor": self._actor_display_name(str(prev.get("actor_id") or "")),
                "text": str(prev.get("content") or "")[:4000],
                "at": str(prev.get("occurred_at") or "")[:80],
            }
            if att_names:
                user_entry["attachments"] = att_names
            items.append(user_entry)
            ob = self.plane.store.fetchone(
                "SELECT payload, created_at FROM outbox WHERE idempotency_key=? AND status IN ('sent','sending','pending')",
                (f"qq-publication:{prev.get('event_id')}",),
            )
            if ob is not None:
                try:
                    ob_payload = self.plane.store.decode(ob["payload"])
                    reply_body = str(ob_payload.get("body") or "").strip()
                except (TypeError, ValueError):
                    reply_body = ""
                if reply_body:
                    items.append({
                        "role": "assistant",
                        "actor": "company-agent",
                        "text": reply_body[:4000],
                        "at": str(ob["created_at"] or "")[:80],
                    })
        return items[-20:]

    def _classify_message(self, event: Event, destination: dict[str, str]) -> str:
        if event.source_type in {"qq.c2c", "qq.direct", "qq.private"}:
            return "direct"
        if destination.get("mentions_bot") == "1":
            return "direct"
        text = event.content.strip()
        if not text:
            return "chatter"
        if "<@!" in text:
            return "direct"

        upstream = getattr(self.workflow, "upstream", None)
        classify_fn = getattr(upstream, "classify_message", None)
        if not callable(classify_fn):
            # Test workflow stubs without a classifier route directly only when ambient listening is disabled.
            return "ongoing_discussion" if self.ambient_listen_enabled() else "direct"

        try:
            recent = self._recent_classify_context(event)
            result = classify_fn(
                source_type=event.source_type,
                conversation_id=event.conversation_id,
                actor_id=event.actor_id,
                actor_label=self._actor_display_name(event.actor_id),
                content=event.content,
                has_attachments=bool(event.attachments),
                attachments=event.attachments,
                bot_aliases=list(self.bot_aliases()),
                recent_messages=recent,
            )
            classification = (
                getattr(result, "classification", None)
                or (result.get("classification") if isinstance(result, dict) else None)
            )
            if classification in {"direct", "ongoing_discussion", "chatter"}:
                return classification
        except (DssuperAgentError, Exception) as error:
            print(f"company-agent: DSSuperAgent classify unavailable ({type(error).__name__}); deferring safely",
                  file=sys.stderr)
        return "ongoing_discussion" if self.ambient_listen_enabled() else "chatter"

    def _env_admin_ids(self) -> frozenset[str]:
        if self.admin_user_ids is not None:
            return self.admin_user_ids
        if self.knowledge_service is not None:
            return self.knowledge_service.settings.admin_user_ids
        return frozenset()

    def _workspace_for_event(self, event: Event) -> str | None:
        if not self.settings.auto_execute_capabilities:
            return None
        try:
            existing_run = self.plane.get_assistant_run(event.event_id)
        except NotFound:
            existing_run = None
        if existing_run is not None and existing_run.workspace_id:
            return existing_run.workspace_id
        manager = self.workspace_manager or getattr(self.workflow, "manager", None)
        if manager is None or not getattr(self.settings, "isolate_workspaces", True):
            return self.settings.workspace_id
        if event.source_type in {"qq.group", "qq.guild"}:
            group = self.identities.get_group(event.source_type, event.source_id)
            digest = hashlib.sha256(f"{event.source_type}:{event.source_id}".encode("utf-8")).hexdigest()[:12]
            if group is not None and group.tier == "internal":
                workspace_id, tier = f"ws-internal-{digest}", "internal"
            else:
                workspace_id, tier = f"ws-client-{digest}", "client_group"
        elif event.source_type in {"qq.direct", "qq.private", "qq.c2c"}:
            role = self.identities.user_role(event.actor_id, self._env_admin_ids())
            digest = hashlib.sha256(f"dm:{event.conversation_id}".encode("utf-8")).hexdigest()[:12]
            if role == "admin":
                workspace_id, tier = f"ws-admin-{digest}", "admin"
            elif role == "employee":
                workspace_id, tier = f"ws-emp-{digest}", "internal"
            else:
                workspace_id, tier = f"ws-dm-{digest}", "direct"
        else:
            return self.settings.workspace_id
        manager.provision(workspace_id, tier=tier)
        return workspace_id

    def _handle_slash_command(self, event: Event, destination: dict[str, str]) -> bool:
        raw = event.content.strip()
        if not raw:
            return False
        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd not in {"/bind", "/mark-internal", "/mark-client", "/compat", "/whoami", "/internal", "/save-internal"}:
            return False
        digest = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:24]
        idempotency_key = f"qq-publication:{event.event_id}"
        if self.plane.store.fetchone(
            "SELECT 1 FROM publication_decisions WHERE idempotency_key=?", (idempotency_key,)
        ):
            return True

        reply: str | None = None
        if cmd == "/bind":
            if not arg:
                reply = "请在命令后附上绑定码，例如：/bind ABCD-1234"
            else:
                external_id = (event.external_actor_id or event.actor_id).strip()
                try:
                    bound = self.identities.redeem_bind_code(
                        arg, provider="qq", account_id=self.settings.app_id,
                        external_id=external_id, scope_id="*",
                    )
                except ValueError as error:
                    reply = f"绑定失败：{error}"
                else:
                    role_names = {"admin": "企业管理员 (L3)", "employee": "企业员工 (L2)", "customer": "客户 (L1)"}
                    role_label = role_names.get(bound["role"], bound["role"])
                    reply = f"绑定成功！当前账号已关联「{bound['display_name']}」，角色：{role_label}。"
        elif cmd in {"/mark-internal", "/mark-client"}:
            if event.source_type not in {"qq.group", "qq.guild"}:
                reply = "请在需要设置的 QQ 群内发送该命令。"
            elif not self.identities.is_admin(event.actor_id, self._env_admin_ids()):
                reply = "权限不足：仅企业管理员可变更群聊级别，当前群保持 L1 客户隔离层。"
            else:
                target_tier = "internal" if cmd == "/mark-internal" else "client_group"
                self.identities.update_group(source_type=event.source_type, source_id=event.source_id, tier=target_tier)
                reply = (
                    "已将本群标记为「企业内部群 (L2)」，可共享公司内部知识库与内部工具。"
                    if target_tier == "internal"
                    else "已将本群设为「客户群 (L1 隔离)」，仅可访问本群资料及公共工具。"
                )
        elif cmd == "/compat":
            if event.source_type not in {"qq.group", "qq.guild"}:
                reply = "请在 QQ 群内发送 /compat on | off | inherit（或在管理台设置）。"
            elif not self.identities.is_admin(event.actor_id, self._env_admin_ids()):
                reply = "权限不足：仅企业管理员可修改群聊向下兼容开关。"
            elif arg.lower() not in {"on", "off", "inherit"}:
                reply = "用法：/compat on | off | inherit"
            else:
                self.identities.update_group(
                    source_type=event.source_type, source_id=event.source_id, downward_compat=arg.lower()
                )
                reply = f"已更新本群向下兼容模式：{arg.lower()}。"
        elif cmd == "/whoami":
            role = self.identities.user_role(event.actor_id, self._env_admin_ids())
            role_names = {"admin": "企业管理员 (L3)", "employee": "企业员工 (L2)", "customer": "客户/未绑定 (L1)"}
            if event.source_type in {"qq.group", "qq.guild"}:
                group = self.identities.get_group(event.source_type, event.source_id)
                tier_desc = "企业内部群 (L2)" if (group and group.tier == "internal") else "客户隔离群 (L1，受房间天花板限制)"
                reply = f"身份：{role_names.get(role, role)}\n当前场景：{tier_desc}"
            else:
                reply = f"身份：{role_names.get(role, role)}\n当前场景：私聊"
        elif cmd in {"/internal", "/save-internal"}:
            group_locked = False
            if event.source_type in {"qq.group", "qq.guild"}:
                group = self.identities.get_group(event.source_type, event.source_id)
                group_locked = group is None or group.tier != "internal"
            if not self.identities.is_internal_user(event.actor_id, self._env_admin_ids()):
                reply = "权限不足：仅企业员工或管理员可将资料存入企业内部共享知识库。请先联系管理员生成绑定码并发送 /bind。"
            elif group_locked:
                reply = "安全隔离限制：客户群 (L1) 内禁止写入企业内部共享知识库。请私聊机器人或在内部群内发送 /internal。"
            elif event.attachments and not arg:
                reply = "已收到附件，将作为「企业内部共享资料」入库（3 个内部群与员工私聊可用）。"
            elif not arg:
                reply = "请附上要存入企业内部知识库的文本内容或附件，例如：/internal 公司新报价规则..."
            elif event.attachments:
                return False
            elif self.knowledge_service is None:
                reply = "当前未启用知识库服务，无法写入内部资料。"
            else:
                saved = self.knowledge_service.ingest_internal_text(
                    actor_id=event.actor_id, title=arg[:40], text=arg, event_id=event.event_id,
                )
                reply = f"已存入企业内部共享知识库（{saved['filename']}），3 个内部群与员工均可检索使用。"

        if reply is None:
            return False
        self._publish_command_reply(event, destination, body=reply, decision_id=f"qq-command-{digest}",
                                    idempotency_key=idempotency_key)
        return True

    def _publish_command_reply(self, event: Event, destination: dict[str, str], *, body: str,
                               decision_id: str, idempotency_key: str) -> None:
        allowed = "group_update" in self.settings.allowed_publications
        disposition = "group_update" if allowed else "suppress"
        try:
            run = self.plane.get_assistant_run(event.event_id)
            conversation_id = run.conversation_id or event.conversation_id
        except NotFound:
            if hasattr(self.workflow, "_local_thread_ids"):
                conversation_id, _ = self.workflow._local_thread_ids(event)
            else:
                conversation_id = event.conversation_id
            try:
                self.plane.get_conversation(conversation_id)
            except NotFound:
                self.plane.create_conversation(
                    conversation_id=conversation_id,
                    kind=event.source_type,
                    participant_refs=[event.actor_id],
                    policy_version=getattr(getattr(self.workflow, "policy", None), "version", "qq-command-v1"),
                )
        self.plane.decide_publication(
            decision_id=decision_id,
            work_item_id=None,
            conversation_id=conversation_id,
            disposition=disposition,
            reason="QQ channel self-service command response",
            policy_version="qq-command-v1",
            body=body[:1800] if allowed else "",
            idempotency_key=idempotency_key,
            destination=destination if allowed else None,
        )

    def process_prepared_attachments(self, event: Event) -> None:
        self.attachment_ingestor.process_event(event, on_update=self.plane.update_event)

    def resume_pending_attachments(self) -> None:
        for event_id in self.attachment_ingestor.pending_event_ids():
            try:
                event = self.plane.get_event(event_id)
                self.process_prepared_attachments(event)
            except Exception as error:
                print(f"company-agent: attachment recovery failed ({type(error).__name__})", file=sys.stderr)

    def _publish_processing_failure(self, event: Event, destination: dict[str, str], category: str | None) -> None:
        allowed = "group_update" in self.settings.allowed_publications
        disposition = "group_update" if allowed else "suppress"
        digest = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:24]
        try:
            run = self.plane.get_assistant_run(event.event_id)
            conversation_id = run.conversation_id or event.conversation_id
        except NotFound:
            if hasattr(self.workflow, "_local_thread_ids"):
                conversation_id, _ = self.workflow._local_thread_ids(event)
            else:
                conversation_id = event.conversation_id
            policy_version = getattr(getattr(self.workflow, "policy", None), "version", "qq-fallback-v1")
            try:
                self.plane.get_conversation(conversation_id)
            except NotFound:
                self.plane.create_conversation(
                    conversation_id=conversation_id,
                    kind=event.source_type,
                    participant_refs=[event.actor_id],
                    policy_version=policy_version,
                )
            self.plane.ingest_event(event)
        self.plane.decide_publication(
            decision_id=f"qq-processing-failed-{digest}",
            work_item_id=None,
            conversation_id=conversation_id,
            disposition=disposition,
            reason=f"QQ event processing failed ({category or 'unknown'})",
            policy_version="qq-processing-failure-v1",
            body="这条消息已收到，但本次处理未能完成，因此没有生成可交付的结论。请稍后重新发送请求。",
            idempotency_key=f"qq-processing-failure:{event.event_id}",
            destination=destination if allowed else None,
        )

    def _resolve_publication(self, recommendation: Any) -> tuple[str, str, str] | None:
        if not isinstance(recommendation, dict):
            return None
        disposition = recommendation.get("recommendation")
        if disposition not in {"suppress", "private_nudge", "group_update", "urgent_escalation"}:
            return None
        chosen = disposition if disposition in self.settings.allowed_publications else "suppress"
        policy_note = ""
        if chosen == "suppress" and disposition == "private_nudge" and "group_update" in self.settings.allowed_publications:
            chosen = "group_update"
            policy_note = "publication fallback: private_nudge -> group_update because QQ private publication is unavailable"
        elif chosen == "suppress" and disposition != "suppress":
            policy_note = f"publication blocked: {disposition} is not allowlisted for this QQ channel"
        return chosen, policy_note, disposition

    def _on_execution_started(self, event: Event, run: Any, item: Any) -> None:
        if not event.source_type.startswith("qq."):
            return
        pub = getattr(run, "publication", None)
        decision = getattr(run, "decision", None)
        intent = decision.get("assistant_intent") if isinstance(decision, dict) else None
        if (isinstance(pub, dict) and pub.get("silent_ack") is True) or (
            isinstance(intent, dict) and intent.get("silent_ack") is True
        ):
            return
        with self._inflight_destinations_lock:
            destination = self._inflight_destinations.get(event.event_id)
        if not destination:
            return
        resolved = self._resolve_publication(getattr(run, "publication", None))
        if resolved is None:
            return
        chosen, policy_note, _ = resolved
        if chosen == "suppress":
            return
        title = (getattr(item, "title", "") or "请求").strip()[:80]
        body = f"已收到任务「{title}」，正在执行处理中；耗时较长时会在完成后推送结果。"
        digest = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:24]
        reason = "Long-running task acknowledgement"
        if policy_note:
            reason = f"{reason}; {policy_note}"
        self.plane.decide_publication(
            decision_id=f"qq-ack-{digest}",
            work_item_id=getattr(run, "work_item_id", None),
            conversation_id=getattr(run, "conversation_id", None) or event.conversation_id,
            disposition=chosen,
            reason=reason,
            policy_version=str(getattr(run, "policy_version", "qq-channel-v1")),
            body=body,
            idempotency_key=f"qq-ack:{event.event_id}",
            destination=destination,
        )
        self.flush_outbox()

    def _decide_publication(self, event: Event, run: dict[str, Any], destination: dict[str, str]) -> None:
        status = run.get("status")
        if status not in {"observed", "completed", "execution_failed"}:
            return
        recommendation = run.get("publication")
        work_item_id = run.get("work_item_id")
        if (not isinstance(recommendation, dict)
                or (status in {"completed", "execution_failed"} and not isinstance(work_item_id, str))
                or (work_item_id is not None and not isinstance(work_item_id, str))):
            return
        resolved = self._resolve_publication(recommendation)
        if resolved is None:
            return
        chosen, policy_note, requested_disposition = resolved
        is_silent_ambient = bool(
            (isinstance(recommendation, dict) and recommendation.get("silent_ack"))
            or (((run.get("decision") or {}).get("assistant_intent") or {}).get("silent_ack"))
        )
        if status == "execution_failed" and is_silent_ambient:
            chosen = "suppress"
            policy_note = "suppressed execution_failed notice for silent ambient task"
        publishable = chosen != "suppress"
        body = recommendation.get("body") if publishable else ""
        if not isinstance(body, str):
            body = ""
        deliverables: list[dict[str, Any]] = []
        if publishable and status == "observed":
            body = body or "这条消息已收到，当前没有形成需要执行的任务。"
        elif publishable and status == "completed":
            result = self._completed_artifact_text(run)
            deliverables = self._completed_deliverables(run)
            if result:
                body = "处理完成：\n" + result
            elif deliverables:
                names = "、".join(item["filename"] for item in deliverables)
                body = f"处理完成，正在上传交付文件：{names}"
            else:
                body = "处理未完成：没有生成可投递的最终答复，因此未发送执行说明。"
        elif publishable and status == "execution_failed":
            detail = str(run.get("error") or "执行失败")[:500]
            body = f"这项任务没有完成，失败阶段：{detail}"
        body = body[:1800]
        digest = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:24]
        reason = str(recommendation.get("reason", ""))[:1000] or "QQ channel publication policy"
        if policy_note:
            reason = f"{reason}; {policy_note} (requested={requested_disposition}, chosen={chosen})"
        self.plane.decide_publication(
            decision_id=f"qq-publication-{digest}",
            work_item_id=work_item_id,
            conversation_id=run.get("conversation_id") or event.conversation_id,
            disposition=chosen,
            reason=reason,
            policy_version=str(run.get("policy_version", "qq-channel-v1")),
            body=body,
            idempotency_key=f"qq-publication:{event.event_id}",
            destination=destination if publishable else None,
        )
        for index, file_meta in enumerate(deliverables, start=1):
            self.plane.decide_publication(
                decision_id=f"qq-file-{digest}-{index}",
                work_item_id=work_item_id,
                conversation_id=run.get("conversation_id") or event.conversation_id,
                disposition=chosen,
                reason=f"{reason} (file deliverable {index}: {file_meta['filename']})"[:1000],
                policy_version=str(run.get("policy_version", "qq-channel-v1")),
                body=f"[文件] {file_meta['filename']}",
                idempotency_key=f"qq-file:{event.event_id}:{index}",
                destination=destination,
                attachments=[file_meta],
            )

    def _completed_deliverables(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        manager = self.workspace_manager or getattr(self.workflow, "manager", None)
        workspace_id = run.get("workspace_id")
        if manager is None or not isinstance(workspace_id, str) or not workspace_id:
            return []
        collected: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for result in reversed(run.get("execution_results", [])):
            if not isinstance(result, dict) or result.get("status") != "succeeded":
                continue
            files = result.get("deliverable_files")
            if not isinstance(files, list):
                continue
            for item in files:
                if not isinstance(item, dict):
                    continue
                rel = item.get("path")
                if not isinstance(rel, str) or not rel or rel in seen_paths:
                    continue
                try:
                    resolved = resolve_workspace_file(manager, workspace_id, rel)
                except (OSError, ValueError):
                    continue
                seen_paths.add(rel)
                collected.append({
                    "workspace_id": workspace_id,
                    "path": rel,
                    "filename": str(item.get("filename") or resolved.name)[:200],
                    "bytes": resolved.stat().st_size,
                    "sha256": str(item.get("sha256") or "")[:128],
                    "content_type": str(item.get("content_type") or "application/octet-stream")[:120],
                })
                if len(collected) >= 5:
                    return collected
            if collected:
                break
        return collected

    def _completed_artifact_text(self, run: dict[str, Any]) -> str | None:
        manager = getattr(self.workflow, "manager", None)
        workspace_id = run.get("workspace_id")
        if manager is None or not isinstance(workspace_id, str):
            return None
        try:
            root = manager.get(workspace_id).root.resolve(strict=True)
        except (OSError, ValueError):
            return None
        for result in reversed(run.get("execution_results", [])):
            if not isinstance(result, dict) or result.get("status") != "succeeded":
                continue
            execution_id = result.get("execution_id")
            if not isinstance(execution_id, str):
                continue
            refs = result.get("artifact_refs")
            if not isinstance(refs, list):
                continue
            for reference in reversed(refs):
                if not isinstance(reference, str) or "\\" in reference or ":" in reference:
                    continue
                relative = PurePosixPath(reference)
                if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                    continue
                if relative.suffix.lower() not in {".md", ".txt"}:
                    continue
                candidate = root.joinpath(*relative.parts)
                current = candidate
                while current != root and current.is_relative_to(root):
                    if current.is_symlink():
                        break
                    current = current.parent
                else:
                    try:
                        resolved = candidate.resolve(strict=True)
                        if (not resolved.is_relative_to(root) or not resolved.is_file()
                                or resolved.stat().st_size > 64 * 1024):
                            continue
                        content = resolved.read_text(encoding="utf-8").strip()
                    except (OSError, UnicodeError, ValueError):
                        continue
                    reply = extract_final_reply(content, execution_id)
                    if reply:
                        return reply
        return None

    def flush_outbox(self, *, respect_backoff: bool = False, max_attempts: int = 12) -> None:
        with self._outbox_lock:
            now_epoch = time.time()
            for pending in self.plane.list_outbox():
                if pending.status not in {"pending", "failed"} or pending.destination.get("channel") != "qq":
                    continue
                if pending.publication_disposition not in self.settings.allowed_publications:
                    self.plane.cancel_outbox(pending.outbox_id, "QQ publication is no longer allowlisted")
                    continue
                if respect_backoff and pending.status == "failed":
                    if pending.attempts >= max_attempts:
                        continue
                    if not self._retry_due(pending, now_epoch):
                        continue
                try:
                    sending = self.plane.mark_outbox_attempt(pending.outbox_id)
                except InvalidTransition:
                    continue
                try:
                    if sending.attachments:
                        att = sending.attachments[0]
                        manager = self.workspace_manager or getattr(self.workflow, "manager", None)
                        if manager is None:
                            raise RuntimeError("WorkspaceManager is unavailable for QQ file delivery")
                        ws_id = str(att.get("workspace_id") or "")
                        rel_path = str(att.get("path") or "")
                        filename = str(att.get("filename") or "") or None
                        resolved = resolve_workspace_file(manager, ws_id, rel_path)
                        touch_workspace_files(manager, ws_id, [rel_path])
                        if "user_openid" in sending.destination:
                            self.api.send_c2c_file(sending, file_path=resolved, filename=filename)
                        else:
                            self.api.send_group_file(sending, file_path=resolved, filename=filename)
                    elif "user_openid" in sending.destination:
                        self.api.send_c2c_message(sending)
                    else:
                        self.api.send_group_message(sending)
                except Exception as error:
                    self.plane.mark_outbox_failed(sending.outbox_id, str(error))
                    print(f"company-agent: QQ Outbox delivery failed ({sending.outbox_id}): {str(error)[:240]}",
                          file=sys.stderr)
                else:
                    self.plane.mark_outbox_sent(sending.outbox_id)

    @staticmethod
    def _retry_due(message: OutboxMessage, now_epoch: float) -> bool:
        if not message.last_attempt_at:
            return True
        try:
            last_attempt = datetime.fromisoformat(message.last_attempt_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if last_attempt.tzinfo is None:
            last_attempt = last_attempt.replace(tzinfo=timezone.utc)
        delay = min(300.0, 15.0 * (2 ** max(0, message.attempts - 1)))
        return (now_epoch - last_attempt.timestamp()) >= delay


class QQGateway:
    MAX_IN_FLIGHT_EVENTS = 64

    def __init__(self, api: QQBotAPI, on_event: Callable[..., None], *, reconnect_delay: float = 2,
                 on_status: Callable[[str], None] | None = None,
                 on_intake: Callable[[str, dict[str, Any]], tuple[Event, dict[str, str]] | None] | None = None,
                 on_attachments: Callable[[Event], None] | None = None):
        self.api = api
        self.on_event = on_event
        self.reconnect_delay = reconnect_delay
        self.on_status = on_status
        self.on_intake = on_intake
        self.on_attachments = on_attachments
        self.sequence: int | None = None
        self.session_id: str | None = None
        self._event_tasks: set[asyncio.Task[None]] = set()

    def _dispatch_event(self, event_type: str, payload: dict[str, Any]) -> asyncio.Task[None] | None:
        if len(self._event_tasks) >= self.MAX_IN_FLIGHT_EVENTS:
            raise RuntimeError("QQ event processing is at capacity")
        if self.on_intake:
            try:
                prepared = self.on_intake(event_type, payload)
            except Exception as error:
                print(f"company-agent: QQ event intake failed ({type(error).__name__})", file=sys.stderr)
                return None
            if prepared is None:
                return None
            event, destination = prepared
            task = asyncio.create_task(asyncio.to_thread(self.on_event, event, destination))
            self._track_task(task)
            if self.on_attachments and event.attachments:
                attachment_task = asyncio.create_task(asyncio.to_thread(self.on_attachments, event))
                self._track_task(attachment_task)
            return task
        task = asyncio.create_task(asyncio.to_thread(self.on_event, event_type, payload))
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
            print(f"company-agent: QQ event worker failed ({type(error).__name__})", file=sys.stderr)

    @staticmethod
    def _identify(token: str) -> dict[str, Any]:
        return {"op": 2, "d": {"token": f"QQBot {token}", "intents": QQ_GROUP_AND_C2C_MESSAGE_INTENT, "shard": [0, 1]}}

    def _resume(self, token: str) -> dict[str, Any]:
        return {"op": 6, "d": {"token": f"QQBot {token}", "session_id": self.session_id, "seq": self.sequence}}

    async def run_forever(self) -> None:
        import websockets

        while True:
            heartbeat = None
            try:
                async with websockets.connect(self.api.gateway_url(), ping_interval=None, close_timeout=5) as socket:
                    heartbeat_ack = asyncio.Event()
                    try:
                        async for raw in socket:
                            frame = json.loads(raw)
                            if not isinstance(frame, dict) or not isinstance(frame.get("op"), int):
                                raise RuntimeError("QQ gateway sent an invalid frame")
                            op = frame["op"]
                            data = frame.get("d")
                            if op == 10:
                                interval = data.get("heartbeat_interval") if isinstance(data, dict) else None
                                if not isinstance(interval, (int, float)) or interval <= 0:
                                    raise RuntimeError("QQ gateway HELLO has no valid heartbeat interval")
                                token = self.api.access_token()
                                identify = self._resume(token) if self.session_id else self._identify(token)
                                await socket.send(json.dumps(identify))
                                heartbeat = asyncio.create_task(
                                    self._heartbeat(socket, interval / 1000, heartbeat_ack)
                                )
                            elif op == 0:
                                sequence = frame.get("s")
                                event_type = frame.get("t")
                                if event_type == "READY" and isinstance(data, dict):
                                    self.session_id = data.get("session_id") or self.session_id
                                if event_type in {"READY", "RESUMED"} and self.on_status:
                                    self.on_status("connected")
                                if event_type != "RESUMED" and isinstance(event_type, str) and isinstance(data, dict):
                                    self._dispatch_event(event_type, data)
                                if isinstance(sequence, int):
                                    self.sequence = sequence
                            elif op == 1:
                                await socket.send(json.dumps({"op": 1, "d": self.sequence}))
                            elif op == 7:
                                raise ConnectionError("QQ gateway requested reconnect")
                            elif op == 9:
                                if data is False:
                                    self.session_id = None
                                    self.sequence = None
                                raise ConnectionError("QQ gateway invalidated the current session")
                            elif op == 11:
                                heartbeat_ack.set()
                                if self.on_status:
                                    self.on_status("connected")
                    finally:
                        if heartbeat:
                            heartbeat.cancel()
                            await asyncio.gather(heartbeat, return_exceptions=True)
                await asyncio.sleep(self.reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self.on_status:
                    self.on_status("reconnecting")
                print(f"company-agent: QQ gateway reconnecting after {type(error).__name__}", file=sys.stderr)
                await asyncio.sleep(self.reconnect_delay)

    async def _heartbeat(self, socket: Any, interval: float, acknowledgement: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(interval)
            acknowledgement.clear()
            await socket.send(json.dumps({"op": 1, "d": self.sequence}))
            try:
                await asyncio.wait_for(acknowledgement.wait(), timeout=interval)
            except TimeoutError:
                await socket.close()
                return
