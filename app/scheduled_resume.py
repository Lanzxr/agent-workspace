from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import Event
from .scheduler import ScheduledTask


class ScheduledResumeHandler:
    """Turn a due schedule into a durable event in the source conversation."""

    def __init__(self, channel: Any) -> None:
        self.channel = channel

    def _resolve_channel(self, channel_key: str | None) -> Any:
        if isinstance(self.channel, Mapping):
            if channel_key and channel_key in self.channel:
                return self.channel[channel_key]
            return next(iter(self.channel.values()), None)
        return self.channel

    def __call__(self, task: ScheduledTask) -> None:
        payload = task.payload
        destination = payload.get("destination") or {}
        channel_key = destination.get("channel") if isinstance(destination, dict) else None
        target = self._resolve_channel(channel_key)
        if target is None:
            return
        saved = payload.get("context", {}).get("source_event")
        if not isinstance(saved, dict):
            saved = target.plane.get_event(payload["source_event_id"]).to_dict()
        source = Event.from_dict(saved)
        context = payload.get("context", {})
        summary = context.get("summary", "") if isinstance(context, dict) else ""
        continuation = "\n\n".join(part for part in (
            f"续接提示：{payload['resume_prompt']}",
            f"关联工作项：{payload['work_item_id']}",
            f"已保存上下文：{summary}" if summary else "",
            "来源消息过长，定时任务快照已截断。" if context.get("source_event_truncated") else "",
            f"来源消息：\n{source.content[:24000]}",
        ) if part)
        event = Event(
            event_id=f"schedule-wake:{task.schedule_id}",
            source_type=source.source_type,
            source_id=source.source_id,
            conversation_id=source.conversation_id,
            actor_id=source.actor_id,
            occurred_at=task.due_at,
            content=continuation[:32000],
            attachments=[],
            capabilities=source.capabilities,
            external_actor_id=source.external_actor_id,
        )
        run = target.workflow.process_event(event)
        target_name = getattr(target, "channel_name", None)
        if (channel_key == "qq" and target_name is None) or (target_name is not None and channel_key == target_name):
            target._decide_publication(event, run.to_dict(), destination)
            target.flush_outbox()
