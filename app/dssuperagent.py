from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .models import Event

DEFAULT_DECISION_TIMEOUT_SECONDS = 600


class DssuperAgentError(RuntimeError):
    category = "dssuperagent_error"


class DssuperAgentUnavailable(DssuperAgentError):
    category = "upstream_unavailable"


class DssuperAgentRejected(DssuperAgentError):
    category = "upstream_rejected"


class DssuperAgentTimeout(DssuperAgentError):
    category = "decision_timeout"


class DssuperAgentProtocolError(DssuperAgentError):
    category = "protocol_error"


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request: Request, file_pointer: Any, code: int, message: str,
                         headers: Any, new_url: str) -> None:
        return None


@dataclass(frozen=True)
class DssuperAgentReceipt:
    status: str
    trigger_id: str
    thread_key: str


@dataclass(frozen=True)
class DssuperAgentDecision:
    decision_id: str
    trigger_ids: list[str]
    thread_key: str
    kind: str
    outcome: str
    reason: Any
    occurred_at: str
    strength: str
    assistant_intent: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DssuperAgentClassifyResult:
    classification: str
    reason: str
    model: str = ""


class DssuperAgentAdapter:
    """Loopback adapter for the pinned DSSuperAgent Company Agent bridge."""

    def __init__(self, base_url: str, *, timeout: float = 10, poll_interval: float = 1) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("DSSuperAgent URL must be an HTTP(S) loopback origin without credentials or a path")
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("DSSuperAgent timeout values must be positive")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._opener = build_opener(ProxyHandler({}), _NoRedirectHandler())

    def submit_event(self, event: Event, *, context: list[dict[str, Any]] | None = None,
                     interaction_mode: str | None = None,
                     available_capabilities: dict[str, Any] | None = None) -> DssuperAgentReceipt:
        self._validate_event(event, context or [], interaction_mode=interaction_mode)
        value = event.to_dict()
        value.pop("external_actor_id", None)
        if event.knowledge_grant:
            value["knowledge_grant"] = event.knowledge_grant
        if interaction_mode is not None:
            value["interaction_mode"] = interaction_mode
        if available_capabilities is not None:
            value["available_capabilities"] = available_capabilities
        value["context"] = context or []
        body = self._request("POST", "/orchestrator/api/company-agent/events", value)
        status = body.get("status")
        trigger_id = body.get("trigger_id")
        thread_key = body.get("thread")
        if status not in {"stored", "duplicate", "filtered"} or not isinstance(trigger_id, str) or not isinstance(thread_key, str):
            raise DssuperAgentProtocolError("DSSuperAgent returned an invalid event receipt")
        return DssuperAgentReceipt(status, trigger_id, thread_key)

    def wait_for_decision(self, thread_key: str, trigger_id: str, *, timeout: float = 120) -> DssuperAgentDecision:
        if not thread_key.strip() or not trigger_id.strip() or timeout <= 0:
            raise ValueError("thread_key, trigger_id and a positive timeout are required")
        deadline = time.monotonic() + timeout
        path = f"/orchestrator/api/threads/{quote(thread_key, safe='')}"
        while True:
            try:
                detail = self._request("GET", path)
            except DssuperAgentUnavailable as error:
                if getattr(error, "status", None) != 404 or time.monotonic() >= deadline:
                    raise
                detail = {}
            decisions = detail.get("decisions", [])
            if not isinstance(decisions, list):
                raise DssuperAgentProtocolError("DSSuperAgent thread response has no decision list")
            for decision in decisions:
                if isinstance(decision, dict) and trigger_id in decision.get("triggerIds", []):
                    return self._parse_decision(decision, thread_key, trigger_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DssuperAgentTimeout(f"DSSuperAgent did not decide trigger {trigger_id} before the deadline")
            time.sleep(min(self.poll_interval, remaining))

    def submit_execution_result(self, event: Event, *, context: list[dict[str, Any]] | None = None) -> DssuperAgentReceipt:
        return self.submit_event(event, context=context)

    def classify_message(
        self,
        *,
        source_type: str,
        conversation_id: str,
        actor_id: str,
        content: str,
        actor_label: str = "",
        has_attachments: bool = False,
        attachments: list[dict[str, Any]] | None = None,
        bot_aliases: list[str] | tuple[str, ...] | None = None,
        recent_messages: list[dict[str, Any]] | None = None,
    ) -> DssuperAgentClassifyResult:
        if source_type not in {"qq.group", "qq.guild"}:
            raise ValueError("classify_message is only used for group/guild conversations")
        clean_attachments = [
            {
                **({"filename": str(item["filename"])[:255]} if item.get("filename") else {}),
                **({"content_type": str(item["content_type"])[:120]} if item.get("content_type") else {}),
            }
            for item in (attachments or [])[:10]
            if isinstance(item, dict)
        ]
        clean_recent: list[dict[str, Any]] = []
        for item in (recent_messages or [])[:20]:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role not in {"user", "assistant"}:
                continue
            entry: dict[str, Any] = {
                "role": role,
                "text": str(item.get("text") or "")[:4000],
            }
            if item.get("actor"):
                entry["actor"] = str(item["actor"])[:120]
            if item.get("at"):
                entry["at"] = str(item["at"])[:80]
            if isinstance(item.get("attachments"), list) and item["attachments"]:
                entry["attachments"] = [str(name)[:255] for name in item["attachments"][:10]]
            clean_recent.append(entry)

        payload: dict[str, Any] = {
            "source_type": source_type,
            "conversation_id": conversation_id,
            "actor_id": actor_id,
            "content": str(content)[:12_000],
            "has_attachments": bool(has_attachments or clean_attachments),
            "attachments": clean_attachments,
            "bot_aliases": [str(alias).strip()[:40] for alias in (bot_aliases or [])[:30] if str(alias).strip()],
            "recent_messages": clean_recent,
        }
        if actor_label.strip():
            payload["actor_label"] = actor_label.strip()[:120]
        body = self._request("POST", "/orchestrator/api/company-agent/classify", payload)
        classification = body.get("classification")
        if classification not in {"direct", "ongoing_discussion", "chatter"}:
            raise DssuperAgentProtocolError("DSSuperAgent returned an invalid message classification")
        reason = body.get("reason")
        model = body.get("model")
        return DssuperAgentClassifyResult(
            classification=classification,
            reason=reason.strip() if isinstance(reason, str) and reason.strip() else classification,
            model=model.strip() if isinstance(model, str) else "",
        )

    def _request(self, method: str, path: str, value: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if value is None else json.dumps(value, ensure_ascii=False).encode("utf-8")
        if data is not None and len(data) > 512 * 1024:
            raise DssuperAgentProtocolError("DSSuperAgent request exceeded 512 KiB")
        request = Request(self.base_url + path, data=data, method=method, headers={
            "accept": "application/json",
            "x-orchestrator-request": "1",
            **({"content-type": "application/json; charset=utf-8"} if data is not None else {}),
        })
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
        except HTTPError as error:
            if error.code == 404:
                unavailable = DssuperAgentUnavailable("DSSuperAgent resource is not available yet")
                unavailable.status = error.code
                raise unavailable from error
            if 400 <= error.code < 500 and error.code not in {408, 429}:
                detail = self._http_error_detail(error)
                rejected = DssuperAgentRejected(f"DSSuperAgent rejected the request (HTTP {error.code}): {detail}")
                rejected.status = error.code
                raise rejected from error
            raise DssuperAgentUnavailable(f"DSSuperAgent returned HTTP {error.code}") from error
        except (TimeoutError, URLError, OSError) as error:
            raise DssuperAgentUnavailable(f"DSSuperAgent request failed: {type(error).__name__}") from error
        if len(raw) > 1024 * 1024:
            raise DssuperAgentProtocolError("DSSuperAgent response exceeded 1 MiB")
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise DssuperAgentProtocolError("DSSuperAgent returned invalid JSON") from error
        if not isinstance(body, dict):
            raise DssuperAgentProtocolError("DSSuperAgent response must be a JSON object")
        return body

    @staticmethod
    def _http_error_detail(error: HTTPError) -> str:
        try:
            body = json.loads(error.read(8192))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return "request rejected"
        if not isinstance(body, dict):
            return "request rejected"
        details: list[str] = []
        message = body.get("error")
        if isinstance(message, str) and message.strip():
            details.append(message.strip()[:240])
        issues = body.get("issues")
        if isinstance(issues, list):
            for issue in issues[:8]:
                if not isinstance(issue, dict):
                    continue
                path = issue.get("path")
                reason = issue.get("message")
                if not isinstance(path, list) or not isinstance(reason, str) or not reason.strip():
                    continue
                field = ".".join(str(part)[:40] for part in path[:6])[:120] or "request"
                details.append(f"{field}: {reason.strip()[:160]}")
        return "; ".join(details)[:600] or "request rejected"

    @classmethod
    def _parse_decision(cls, value: dict[str, Any], thread_key: str, trigger_id: str) -> DssuperAgentDecision:
        required_strings = ("id", "kind", "outcome", "at", "strength")
        if any(not isinstance(value.get(name), str) or not value[name] for name in required_strings):
            raise DssuperAgentProtocolError("DSSuperAgent decision is missing required identity fields")
        trigger_ids = value.get("triggerIds")
        if not isinstance(trigger_ids, list) or any(not isinstance(item, str) for item in trigger_ids):
            raise DssuperAgentProtocolError("DSSuperAgent decision has invalid trigger references")
        if trigger_id not in trigger_ids:
            raise DssuperAgentProtocolError("DSSuperAgent decision does not reference the submitted trigger")
        if value["outcome"] not in {"ignore", "wouldAccept", "accept", "unavailable", "stop", "reopen"}:
            raise DssuperAgentProtocolError("DSSuperAgent decision has an unsupported outcome")
        intent = value.get("assistantIntent")
        if not isinstance(intent, dict):
            raise DssuperAgentProtocolError("DSSuperAgent decision has no structured assistant intent")
        cls._validate_intent(intent)
        reason = value.get("reason")
        if not isinstance(reason, (str, dict, list)):
            raise DssuperAgentProtocolError("DSSuperAgent decision has an invalid reason")
        return DssuperAgentDecision(value["id"], trigger_ids, thread_key, value["kind"], value["outcome"], reason,
                                    value["at"], value["strength"], intent)

    @staticmethod
    def _validate_intent(intent: dict[str, Any]) -> None:
        if set(intent) - {
            "action", "rationale", "title", "objective", "completion_conditions",
            "plan_steps", "publication", "silent_ack", "capability_gap",
        }:
            raise DssuperAgentProtocolError("assistant intent contains unsupported fields")
        if "silent_ack" in intent and not isinstance(intent["silent_ack"], bool):
            raise DssuperAgentProtocolError("assistant intent silent_ack must be a boolean")
        if "capability_gap" in intent:
            gap = intent["capability_gap"]
            if (not isinstance(gap, dict)
                    or set(gap) - {"topic", "summary", "missing_capability", "gap_kind", "suggested_solution"}
                    or not isinstance(gap.get("topic"), str) or not gap["topic"].strip() or len(gap["topic"]) > 200
                    or not isinstance(gap.get("summary"), str) or not gap["summary"].strip() or len(gap["summary"]) > 1000
                    or not isinstance(gap.get("missing_capability"), str) or not gap["missing_capability"].strip()
                    or len(gap["missing_capability"]) > 300
                    or not isinstance(gap.get("suggested_solution"), str) or not gap["suggested_solution"].strip()
                    or len(gap["suggested_solution"]) > 1000):
                raise DssuperAgentProtocolError("assistant intent capability_gap is invalid")
            if "gap_kind" in gap and gap["gap_kind"] not in {
                "tool_script", "python_package", "knowledge_access", "external_api", "other"
            }:
                raise DssuperAgentProtocolError("assistant intent capability_gap gap_kind is invalid")
        if intent.get("action") not in {"observe", "propose_work"}:
            raise DssuperAgentProtocolError("assistant intent action must be observe or propose_work")
        if (not isinstance(intent.get("rationale"), str) or not intent["rationale"].strip()
                or len(intent["rationale"]) > 1000):
            raise DssuperAgentProtocolError("assistant intent rationale is required")
        for field in ("completion_conditions", "plan_steps"):
            values = intent.get(field)
            if not isinstance(values, list) or len(values) > 20:
                raise DssuperAgentProtocolError(f"assistant intent {field} must be a list")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 500
               for value in intent["completion_conditions"]):
            raise DssuperAgentProtocolError("work completion conditions must be non-empty strings of at most 500 characters")
        publication = intent.get("publication")
        if (not isinstance(publication, dict) or set(publication) - {"recommendation", "reason", "body"}
                or publication.get("recommendation") not in {
            "suppress", "private_nudge", "group_update", "urgent_escalation"
        } or not isinstance(publication.get("reason"), str) or not publication["reason"].strip()
                or len(publication["reason"]) > 1000):
            raise DssuperAgentProtocolError("assistant intent publication recommendation is invalid")
        if "body" in publication and (not isinstance(publication["body"], str) or len(publication["body"]) > 4000):
            raise DssuperAgentProtocolError("assistant intent publication body must be a string of at most 4000 characters")
        if intent["action"] == "propose_work":
            if (not isinstance(intent.get("title"), str) or not intent["title"].strip() or len(intent["title"]) > 160
                    or not isinstance(intent.get("objective"), str) or not intent["objective"].strip()
                    or len(intent["objective"]) > 4000):
                raise DssuperAgentProtocolError("work proposals require title and objective")
            if not intent["completion_conditions"] or not intent["plan_steps"]:
                raise DssuperAgentProtocolError("work proposals require completion conditions and plan steps")
            for step in intent["plan_steps"]:
                if (not isinstance(step, dict)
                        or set(step) - {"title", "objective", "capability", "completion_conditions",
                                        "knowledge_document_ids", "attachment_ids"}
                        or not isinstance(step.get("title"), str) or not step["title"].strip() or len(step["title"]) > 160
                        or not isinstance(step.get("objective"), str) or not step["objective"].strip() or len(step["objective"]) > 4000
                        or not isinstance(step.get("capability"), str) or not step["capability"].strip() or len(step["capability"]) > 100):
                    raise DssuperAgentProtocolError("each proposed plan step requires title, objective and capability")
                conditions = step.get("completion_conditions", [])
                if (not isinstance(conditions, list) or len(conditions) > 20
                        or any(not isinstance(value, str) or not value.strip() or len(value) > 500 for value in conditions)):
                    raise DssuperAgentProtocolError("plan step completion conditions must be non-empty strings")
                document_ids = step.get("knowledge_document_ids", [])
                if (not isinstance(document_ids, list) or len(document_ids) > 20
                        or any(not isinstance(value, str) or not value.strip() or len(value) > 200
                               for value in document_ids)
                        or len(set(document_ids)) != len(document_ids)):
                    raise DssuperAgentProtocolError("plan step knowledge document IDs are invalid")
                attachment_ids = step.get("attachment_ids", [])
                if (not isinstance(attachment_ids, list) or len(attachment_ids) > 20
                        or any(not isinstance(value, str) or not value.strip() or len(value) > 200
                               for value in attachment_ids)
                        or len(set(attachment_ids)) != len(attachment_ids)):
                    raise DssuperAgentProtocolError("plan step attachment IDs are invalid")

    @staticmethod
    def _validate_event(event: Event, context: list[dict[str, Any]], *, interaction_mode: str | None = None) -> None:
        if interaction_mode is not None and interaction_mode not in {"direct", "ambient_cooldown"}:
            raise DssuperAgentProtocolError("interaction_mode must be direct or ambient_cooldown")
        limits = {
            "event_id": (event.event_id, 200),
            "source_type": (event.source_type, 80),
            "source_id": (event.source_id, 200),
            "conversation_id": (event.conversation_id, 200),
            "actor_id": (event.actor_id, 200),
            "occurred_at": (event.occurred_at, 80),
            "content": (event.content, 12000),
        }
        if any(len(value) > limit for value, limit in limits.values()):
            raise DssuperAgentProtocolError("event exceeds the DSSuperAgent bridge field limits")
        if len(event.attachments) > 20 or len(event.capabilities) > 40:
            raise DssuperAgentProtocolError("event exceeds the DSSuperAgent bridge collection limits")
        if event.knowledge_grant is not None and (not isinstance(event.knowledge_grant, str)
                                                   or not 32 <= len(event.knowledge_grant) <= 256):
            raise DssuperAgentProtocolError("event has an invalid knowledge grant")
        if any(len(value) > 100 for value in event.capabilities):
            raise DssuperAgentProtocolError("event capability exceeds the DSSuperAgent bridge field limit")
        if len(context) > 10:
            raise DssuperAgentProtocolError("event context cannot contain more than ten prior messages")
        allowed_context_fields = {"event_id", "role", "actor_id", "occurred_at", "content"}
        for item in context:
            if (not isinstance(item, dict) or set(item) - allowed_context_fields
                    or not isinstance(item.get("actor_id"), str) or len(item["actor_id"]) > 200
                    or not isinstance(item.get("content"), str) or len(item["content"]) > 12000
                    or "role" in item and item["role"] not in {"user", "assistant"}
                    or "event_id" in item and (not isinstance(item["event_id"], str) or len(item["event_id"]) > 200)
                    or "occurred_at" in item and (not isinstance(item["occurred_at"], str) or len(item["occurred_at"]) > 80)):
                raise DssuperAgentProtocolError("event context does not match the DSSuperAgent bridge schema")
