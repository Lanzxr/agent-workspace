from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import math
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, quote
from urllib.request import Request, urlopen

from .assistant_workflow import DssuperAgentWorkflow
from .attachments import AttachmentIngestor
from .channel_core import BaseChannelService, DEFAULT_AMBIENT_COOLDOWN_SECONDS, DEFAULT_BOT_ALIASES
from .control_plane import ControlPlane
from .identity import IdentityRegistry
from .knowledge.service import KnowledgeService
from .models import Event, OutboxMessage
from .runtime_config import QQBotSettings
from .tool_forge import ForgeSecretsVault, ToolForgeService

QQ_GROUP_AND_C2C_MESSAGE_INTENT = 1 << 25
QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
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


class QQChannelService(BaseChannelService):
    channel_name = "qq"
    provider_name = "qq"
    channel_display_name = "QQ"
    policy_prefix = "qq"
    max_reply_chars = 1800

    def __init__(self, plane: ControlPlane, workflow: DssuperAgentWorkflow, api: QQBotAPI,
                 settings: QQBotSettings, *, identities: IdentityRegistry | None = None,
                 attachment_ingestor: AttachmentIngestor | None = None,
                 knowledge_service: KnowledgeService | None = None,
                 workspace_manager: Any | None = None,
                 admin_user_ids: frozenset[str] | None = None,
                 tool_forge: ToolForgeService | None = None,
                 forge_secrets_vault: ForgeSecretsVault | None = None):
        self.api = api
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
            return None
        if normalized is None:
            return None
        event, destination = normalized
        return self._persist_prepared_event(event, destination)

    def _event_explicitly_mentions_bot(self, event: Event, destination: dict[str, str]) -> bool:
        return destination.get("mentions_bot") == "1" or "<@!" in event.content.strip()

    def _deliver_outbox_message(
        self,
        message: OutboxMessage,
        *,
        file_path: Path | None = None,
        filename: str | None = None,
    ) -> None:
        if file_path is not None:
            if "user_openid" in message.destination:
                self.api.send_c2c_file(message, file_path=file_path, filename=filename)
            else:
                self.api.send_group_file(message, file_path=file_path, filename=filename)
        elif "user_openid" in message.destination:
            self.api.send_c2c_message(message)
        else:
            self.api.send_group_message(message)


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
        self._executor: ThreadPoolExecutor | None = None

    async def _run_worker(self, fn: Callable[..., Any], *args: Any) -> None:
        loop = asyncio.get_running_loop()
        if self._executor is not None:
            await loop.run_in_executor(self._executor, fn, *args)
            return
        await asyncio.to_thread(fn, *args)

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
            task = asyncio.create_task(self._run_worker(self.on_event, event, destination))
            self._track_task(task)
            if self.on_attachments and event.attachments:
                attachment_task = asyncio.create_task(self._run_worker(self.on_attachments, event))
                self._track_task(attachment_task)
            return task
        task = asyncio.create_task(self._run_worker(self.on_event, event_type, payload))
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

        executor = ThreadPoolExecutor(max_workers=self.MAX_IN_FLIGHT_EVENTS, thread_name_prefix="qq-worker")
        self._executor = executor
        try:
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
        finally:
            self._executor = None
            executor.shutdown(wait=False)

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
