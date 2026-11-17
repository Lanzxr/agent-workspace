from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sys
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
from .identity import IdentityRegistry
from .knowledge.service import KnowledgeService
from .models import Event, OutboxMessage
from .reply_contract import extract_final_reply
from .runtime_config import QQBotSettings

QQ_GROUP_AND_C2C_MESSAGE_INTENT = 1 << 25
QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"


def normalize_qq_message(app_id: str, event_type: str, payload: dict[str, Any]) -> tuple[Event, dict[str, str]] | None:
    if event_type not in {"GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"}:
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
                 token: str | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"QQBot {token}"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
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
        body: dict[str, Any] = {"msg_type": 0, "content": message.body}
        _add_passive_reply_reference(body, destination)
        self._request("POST", f"{self.settings.api_base_url}/v2/groups/{quote(group_id, safe='')}/messages",
                      body=body, token=self.access_token())

    def send_c2c_message(self, message: OutboxMessage) -> None:
        destination = message.destination
        user_id = destination.get("user_openid")
        if destination.get("channel") != "qq" or not user_id:
            raise ValueError("Outbox message does not have a QQ C2C destination")
        body: dict[str, Any] = {"msg_type": 0, "content": message.body}
        _add_passive_reply_reference(body, destination)
        self._request("POST", f"{self.settings.api_base_url}/v2/users/{quote(user_id, safe='')}/messages",
                      body=body, token=self.access_token())


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
                 knowledge_service: KnowledgeService | None = None):
        self.plane = plane
        self.workflow = workflow
        self.api = api
        self.settings = settings
        self.identities = identities or IdentityRegistry(plane.store)
        self.knowledge_service = knowledge_service
        self.attachment_ingestor = attachment_ingestor or AttachmentIngestor(
            plane.store, knowledge_service=knowledge_service
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
        event = self.attachment_ingestor.register_event(event)
        if hasattr(self.workflow, "record_event_context"):
            event = self.workflow.record_event_context(event)
        else:
            self.plane.ingest_event(event)
        return event, destination

    def process_prepared_event(self, event: Event, destination: dict[str, str]) -> None:
        if event.attachments and event.content.strip() in {"[附件]", "[空消息]"}:
            return
        try:
            if self.settings.auto_execute_capabilities:
                run = self.workflow.process_event(event, workspace_id=self.settings.workspace_id)
            else:
                run = self.workflow.process_event(event)
            run_data = run.to_dict()
            if run_data.get("status") == "failed":
                self._publish_processing_failure(event, destination, run_data.get("error_category"))
            else:
                self._decide_publication(event, run_data, destination)
        except Exception as error:
            self._publish_processing_failure(event, destination, getattr(error, "category", type(error).__name__))
            print(f"company-agent: QQ event processing failed ({type(error).__name__})", file=sys.stderr)
        self.flush_outbox()

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
            conversation_id, _ = self.workflow._local_thread_ids(event)
            try:
                self.plane.get_conversation(conversation_id)
            except NotFound:
                self.plane.create_conversation(
                    conversation_id=conversation_id,
                    kind=event.source_type,
                    participant_refs=[event.actor_id],
                    policy_version=self.workflow.policy.version,
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
        disposition = recommendation.get("recommendation")
        if disposition not in {"suppress", "private_nudge", "group_update", "urgent_escalation"}:
            return
        allowed = disposition in self.settings.allowed_publications
        chosen = disposition if allowed else "suppress"
        body = recommendation.get("body") if allowed else ""
        if not isinstance(body, str):
            body = ""
        if allowed and status == "observed":
            body = body or "这条消息已收到，当前没有形成需要执行的任务。"
        elif allowed and status == "completed":
            result = self._completed_artifact_text(run)
            body = ("处理完成：\n" + result) if result else (
                "处理未完成：没有生成可投递的最终答复，因此未发送执行说明。"
            )
        elif allowed and status == "execution_failed":
            detail = str(run.get("error") or "执行失败")[:500]
            body = f"这项任务没有完成，失败阶段：{detail}"
        body = body[:1800]
        digest = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:24]
        self.plane.decide_publication(
            decision_id=f"qq-publication-{digest}",
            work_item_id=work_item_id,
            conversation_id=run.get("conversation_id") or event.conversation_id,
            disposition=chosen,
            reason=str(recommendation.get("reason", ""))[:1000] or "QQ channel publication policy",
            policy_version=str(run.get("policy_version", "qq-channel-v1")),
            body=body,
            idempotency_key=f"qq-publication:{event.event_id}",
            destination=destination if allowed else None,
        )

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

    def flush_outbox(self) -> None:
        for pending in self.plane.list_outbox():
            if pending.status not in {"pending", "failed"} or pending.destination.get("channel") != "qq":
                continue
            if pending.publication_disposition not in self.settings.allowed_publications:
                self.plane.cancel_outbox(pending.outbox_id, "QQ publication is no longer allowlisted")
                continue
            try:
                sending = self.plane.mark_outbox_attempt(pending.outbox_id)
            except InvalidTransition:
                continue
            try:
                if "user_openid" in sending.destination:
                    self.api.send_c2c_message(sending)
                else:
                    self.api.send_group_message(sending)
            except Exception as error:
                self.plane.mark_outbox_failed(sending.outbox_id, str(error))
                print(f"company-agent: QQ Outbox delivery failed ({sending.outbox_id})", file=sys.stderr)
            else:
                self.plane.mark_outbox_sent(sending.outbox_id)


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
