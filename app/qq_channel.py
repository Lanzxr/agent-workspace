from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, quote
from urllib.request import Request, urlopen

from .assistant_workflow import DssuperAgentWorkflow
from .control_plane import ControlPlane, InvalidTransition
from .models import Event, OutboxMessage
from .runtime_config import QQBotSettings

QQ_GROUP_MESSAGE_INTENT = 1 << 25


def normalize_group_message(app_id: str, event_type: str, payload: dict[str, Any]) -> tuple[Event, dict[str, str]] | None:
    if event_type != "GROUP_MESSAGE_CREATE":
        return None
    if not isinstance(payload, dict):
        raise ValueError("QQ group message payload must be an object")
    author = payload.get("author")
    if author is None:
        raise ValueError("QQ group message is missing its author")
    if not isinstance(author, dict):
        raise ValueError("QQ group message author must be an object")
    if author.get("bot") is True:
        return None
    message_id = payload.get("id")
    group_id = payload.get("group_openid")
    member_id = author.get("member_openid")
    timestamp = payload.get("timestamp")
    if not all(isinstance(value, str) and value.strip()
               for value in (app_id, message_id, group_id, member_id, timestamp)):
        raise ValueError("QQ group message is missing its id, group, member, or timestamp")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("QQ group message timestamp must be ISO-8601") from error
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError("QQ group message content must be a string")
    raw_attachments = payload.get("attachments", [])
    if not isinstance(raw_attachments, list) or any(not isinstance(item, dict) for item in raw_attachments):
        raise ValueError("QQ group message attachments must be an array of objects")
    attachments = [{key: value for key, value in item.items()
                    if key in {"content_type", "filename", "url"} and isinstance(value, str)}
                   for item in raw_attachments]
    body = content.strip() or ("[附件]" if attachments else "[空消息]")
    event = Event(
        event_id=f"qq:{app_id}:group:{message_id}",
        source_type="qq.group",
        source_id=group_id,
        conversation_id=group_id,
        actor_id=member_id,
        occurred_at=timestamp,
        content=body,
        attachments=attachments,
        capabilities=["dsh.task"],
    )
    return event, {"channel": "qq", "group_openid": group_id, "reply_to_message_id": message_id}


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
        result = self._request("POST", f"{self.settings.api_base_url}/app/getAppAccessToken", body={
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
        reply_id = destination.get("reply_to_message_id")
        if reply_id:
            body["msg_id"] = reply_id
        self._request("POST", f"{self.settings.api_base_url}/v2/groups/{quote(group_id, safe='')}/messages",
                      body=body, token=self.access_token())


class QQChannelService:
    def __init__(self, plane: ControlPlane, workflow: DssuperAgentWorkflow, api: QQBotAPI,
                 settings: QQBotSettings):
        self.plane = plane
        self.workflow = workflow
        self.api = api
        self.settings = settings

    def handle_gateway_event(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            normalized = normalize_group_message(self.settings.app_id, event_type, payload)
        except ValueError as error:
            print(f"company-agent: discarded malformed QQ event ({type(error).__name__})", file=sys.stderr)
            return
        if normalized is None:
            return
        event, destination = normalized
        run = self.workflow.process_event(event)
        self._decide_publication(event, run.to_dict(), destination)
        self.flush_outbox()

    def _decide_publication(self, event: Event, run: dict[str, Any], destination: dict[str, str]) -> None:
        recommendation = run.get("publication")
        work_item_id = run.get("work_item_id")
        if not isinstance(recommendation, dict) or not isinstance(work_item_id, str):
            return
        disposition = recommendation.get("recommendation")
        if disposition not in {"suppress", "private_nudge", "group_update", "urgent_escalation"}:
            return
        allowed = disposition in self.settings.allowed_publications
        chosen = disposition if allowed else "suppress"
        body = recommendation.get("body") if allowed else ""
        if not isinstance(body, str):
            body = ""
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
                self.api.send_group_message(sending)
            except Exception as error:
                self.plane.mark_outbox_failed(sending.outbox_id, str(error))
                print(f"company-agent: QQ Outbox delivery failed ({sending.outbox_id})", file=sys.stderr)
            else:
                self.plane.mark_outbox_sent(sending.outbox_id)


class QQGateway:
    def __init__(self, api: QQBotAPI, on_event: Callable[[str, dict[str, Any]], None], *, reconnect_delay: float = 2):
        self.api = api
        self.on_event = on_event
        self.reconnect_delay = reconnect_delay
        self.sequence: int | None = None
        self.session_id: str | None = None

    @staticmethod
    def _identify(token: str) -> dict[str, Any]:
        return {"op": 2, "d": {"token": f"QQBot {token}", "intents": QQ_GROUP_MESSAGE_INTENT, "shard": [0, 1]}}

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
                                if event_type != "RESUMED" and isinstance(event_type, str) and isinstance(data, dict):
                                    await asyncio.to_thread(self.on_event, event_type, data)
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
                    finally:
                        if heartbeat:
                            heartbeat.cancel()
                            await asyncio.gather(heartbeat, return_exceptions=True)
                await asyncio.sleep(self.reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as error:
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
