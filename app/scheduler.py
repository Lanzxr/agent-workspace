from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from .store import Store


@dataclass(frozen=True)
class ScheduledTask:
    schedule_id: str
    status: str
    due_at: str
    payload: dict
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


class ScheduleService:
    """Persist one-shot wakeups independently from the Linux timer process."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def create(self, *, due_at: str, time_zone: str, source_event_id: str,
               work_item_id: str, resume_prompt: str, context: dict | None = None,
               destination: dict | None = None, schedule_id: str | None = None) -> ScheduledTask:
        if any(not isinstance(value, str) for value in (due_at, time_zone, source_event_id, work_item_id, resume_prompt)):
            raise ValueError("due_at, time_zone, source_event_id, work_item_id and resume_prompt must be strings")
        due = parse_due_time(due_at, time_zone)
        if not source_event_id.strip() or not work_item_id.strip() or not resume_prompt.strip():
            raise ValueError("source_event_id, work_item_id and resume_prompt are required")
        if not self.store.fetchone("SELECT 1 FROM events WHERE event_id=?", (source_event_id,)):
            raise ValueError("source event does not exist")
        if not self.store.fetchone("SELECT 1 FROM work_items WHERE work_item_id=?", (work_item_id,)):
            raise ValueError("work item does not exist")
        if len(resume_prompt) > 4000:
            raise ValueError("resume_prompt exceeds 4000 characters")
        if context is None:
            source = self.store.fetchone("SELECT payload FROM events WHERE event_id=?",
                                         (source_event_id,))
            if source:
                event = self.store.decode(source[0])
                content = event.get("content", "")
                context = {"source_event": {
                    key: event.get(key) for key in ("event_id", "source_type", "source_id", "conversation_id",
                                                    "actor_id", "occurred_at", "capabilities", "external_actor_id")
                } | {"content": content[:16000], "attachments": []},
                    "source_event_truncated": len(content) > 16000}
            else:
                context = {}
        if not isinstance(context, dict) or not isinstance(destination or {}, dict):
            raise ValueError("context and destination must be JSON objects")
        payload = {"time_zone": time_zone, "source_event_id": source_event_id,
                   "work_item_id": work_item_id, "resume_prompt": resume_prompt.strip(),
                   "context": context or {}, "destination": destination or {}}
        if len(json.dumps(payload, ensure_ascii=False)) > 32000:
            raise ValueError("scheduled context exceeds 32 KB")
        now = _now()
        item = ScheduledTask(schedule_id or f"schedule-{uuid4().hex}", "scheduled", due, payload, now, now)
        with self.store.transaction() as connection:
            connection.execute("INSERT INTO scheduled_tasks VALUES (?,?,?,?,?,?)",
                               (item.schedule_id, item.status, item.due_at,
                                json.dumps(payload, ensure_ascii=False), now, now))
            _audit(connection, item.schedule_id, "created", {"due_at": due, "time_zone": time_zone}, now)
        return item

    def list(self, *, limit: int = 200) -> list[ScheduledTask]:
        rows = self.store.fetchall(
            "SELECT * FROM scheduled_tasks ORDER BY due_at LIMIT ?", (max(1, min(limit, 500)),)
        )
        return [_row(row) for row in rows]

    def recover_interrupted(self) -> int:
        """Requeue tasks left running by a process exit; wake event IDs make retries idempotent."""
        now = _now()
        with self.store.transaction() as connection:
            rows = connection.execute("SELECT schedule_id FROM scheduled_tasks WHERE status='running'").fetchall()
            for row in rows:
                connection.execute("UPDATE scheduled_tasks SET status='scheduled',updated_at=? WHERE schedule_id=?",
                                   (now, row["schedule_id"]))
                _audit(connection, row["schedule_id"], "recovered_after_restart", {}, now)
        return len(rows)

    def retry(self, schedule_id: str) -> ScheduledTask:
        now = _now()
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM scheduled_tasks WHERE schedule_id=?", (schedule_id,)).fetchone()
            if row is None:
                raise KeyError(schedule_id)
            if row["status"] != "failed":
                raise ValueError(f"cannot retry task in {row['status']} state")
            connection.execute("UPDATE scheduled_tasks SET status='scheduled',updated_at=? WHERE schedule_id=?",
                               (now, schedule_id))
            _audit(connection, schedule_id, "retry_queued", {}, now)
        return ScheduledTask(schedule_id, "scheduled", row["due_at"], json.loads(row["payload"]), row["created_at"], now)

    def cancel(self, schedule_id: str) -> ScheduledTask:
        now = _now()
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM scheduled_tasks WHERE schedule_id=?", (schedule_id,)).fetchone()
            if row is None:
                raise KeyError(schedule_id)
            task = _row(row)
            if task.status not in {"scheduled", "failed"}:
                raise ValueError(f"cannot cancel task in {task.status} state")
            connection.execute("UPDATE scheduled_tasks SET status='cancelled',updated_at=? WHERE schedule_id=?",
                               (now, schedule_id))
            _audit(connection, schedule_id, "cancelled", {}, now)
        return ScheduledTask(task.schedule_id, "cancelled", task.due_at, task.payload, task.created_at, now)

    def claim_due(self, *, now: datetime | None = None, limit: int = 20) -> list[ScheduledTask]:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        claimed: list[ScheduledTask] = []
        stamp = _now()
        with self.store.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE status='scheduled' AND due_at<=? ORDER BY due_at LIMIT ?",
                (moment, max(1, min(limit, 100))),
            ).fetchall()
            for row in rows:
                connection.execute("UPDATE scheduled_tasks SET status='running',updated_at=? "
                                   "WHERE schedule_id=? AND status='scheduled'", (stamp, row["schedule_id"]))
                claimed.append(ScheduledTask(row["schedule_id"], "running", row["due_at"],
                                             json.loads(row["payload"]), row["created_at"], stamp))
                _audit(connection, row["schedule_id"], "claimed", {}, stamp)
        return claimed

    def finish(self, schedule_id: str, *, error: str | None = None) -> ScheduledTask:
        status = "failed" if error else "completed"
        now = _now()
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM scheduled_tasks WHERE schedule_id=?", (schedule_id,)).fetchone()
            if row is None:
                raise KeyError(schedule_id)
            if row["status"] != "running":
                raise ValueError(f"cannot finish task in {row['status']} state")
            payload = json.loads(row["payload"])
            if error:
                payload["last_error"] = error[:1000]
            connection.execute("UPDATE scheduled_tasks SET status=?,payload=?,updated_at=? WHERE schedule_id=?",
                               (status, json.dumps(payload, ensure_ascii=False), now, schedule_id))
            _audit(connection, schedule_id, status, {"error": error[:500] if error else None}, now)
        return ScheduledTask(schedule_id, status, row["due_at"], payload, row["created_at"], now)


class SchedulerWorker:
    def __init__(self, schedules: ScheduleService, on_wake: Callable[[ScheduledTask], None], *, poll_seconds: float = 5):
        if poll_seconds < 0.25:
            raise ValueError("poll_seconds must be at least 0.25")
        self.schedules = schedules
        self.on_wake = on_wake
        self.poll_seconds = poll_seconds

    def run_once(self) -> int:
        tasks = self.schedules.claim_due()
        for task in tasks:
            try:
                self.on_wake(task)
            except Exception as error:
                self.schedules.finish(task.schedule_id, error=f"{type(error).__name__}: {error}")
            else:
                self.schedules.finish(task.schedule_id)
        return len(tasks)

    async def run_forever(self) -> None:
        while True:
            await asyncio.to_thread(self.run_once)
            await asyncio.sleep(self.poll_seconds)


def parse_due_time(value: str, time_zone: str) -> str:
    zone = ZoneInfo(time_zone)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        first = parsed.replace(tzinfo=zone, fold=0)
        second = parsed.replace(tzinfo=zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise ValueError("local due time is ambiguous or falls in a daylight-saving transition; include an offset")
        parsed = first
    return parsed.astimezone(timezone.utc).isoformat()


def _row(row) -> ScheduledTask:
    return ScheduledTask(row["schedule_id"], row["status"], row["due_at"],
                         json.loads(row["payload"]), row["created_at"], row["updated_at"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(connection, entity_id: str, action: str, payload: dict, now: str) -> None:
    connection.execute("INSERT INTO audit(entity_type,entity_id,action,payload,created_at) VALUES (?,?,?,?,?)",
                       ("scheduled_task", entity_id, action, json.dumps(payload, ensure_ascii=False), now))
