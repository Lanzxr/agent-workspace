from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any

from .store import Store


_RUN_LABELS = {
    "awaiting_decision": "等待 DSSuperAgent 判断",
    "decided": "DSSuperAgent 已返回判断",
    "observed": "DSSuperAgent 已观察",
    "proposed": "DSSuperAgent 已形成工作提案",
    "mapping_proposal": "正在生成工作计划",
    "authorizing": "正在检查执行权限",
    "executing": "DSH 执行中",
    "feedback_pending": "等待 DSH 结果反馈",
    "completed": "处理完成",
    "execution_failed": "DSH 执行失败",
    "failed": "处理失败",
    "filtered": "已忽略",
}

_EXECUTION_LABELS = {
    "queued": "DSH 任务已排队",
    "running": "DSH 正在执行",
    "succeeded": "DSH 执行完成",
    "failed": "DSH 执行失败",
    "timed_out": "DSH 执行超时",
    "cancelled": "DSH 任务已取消",
}

_AUDIT_LABELS = {
    ("event", "ingested"): "Company Agent 已收到消息",
    ("event", "attachments_updated"): "附件状态已更新",
    ("assistant_run", "status:awaiting_decision"): "已提交 DSSuperAgent，等待判断",
    ("assistant_run", "status:decided"): "DSSuperAgent 已返回判断",
    ("assistant_run", "status:observed"): "DSSuperAgent 判定为观察",
    ("assistant_run", "status:proposed"): "DSSuperAgent 已形成工作提案",
    ("assistant_run", "status:mapping_proposal"): "已生成工作计划",
    ("assistant_run", "status:authorizing"): "正在检查执行权限",
    ("assistant_run", "status:executing"): "开始执行 DSH 任务",
    ("assistant_run", "status:feedback_pending"): "等待 DSH 结果反馈",
    ("assistant_run", "status:completed"): "处理完成",
    ("assistant_run", "status:execution_failed"): "DSH 执行失败",
    ("assistant_run", "status:failed"): "处理失败",
    ("assistant_run", "status:filtered"): "消息已被过滤",
    ("work_item", "created"): "已创建 Work",
    ("execution", "queued"): "已创建 DSH 任务",
    ("execution", "started"): "DSH 开始执行",
    ("execution", "result:succeeded"): "DSH 返回成功结果",
    ("execution", "result:failed"): "DSH 返回失败结果",
    ("execution", "result:timed_out"): "DSH 执行超时",
    ("execution", "result:cancelled"): "DSH 任务已取消",
    ("outbox", "sending"): "正在发送回复",
    ("outbox", "sent"): "回复发送成功",
    ("outbox", "failed"): "回复发送失败",
    ("outbox", "cancelled"): "回复已取消",
}


def _decode(store: Store, value: str) -> dict[str, Any]:
    try:
        decoded = store.decode(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _safe_payload(value: Any, *, depth: int = 0) -> Any:
    """Keep detail responses useful without exposing full prompts or large payloads."""
    if depth > 2:
        return "[truncated]"
    if isinstance(value, dict):
        return {str(key): _safe_payload(item, depth=depth + 1) for key, item in list(value.items())[:24]}
    if isinstance(value, list):
        return [_safe_payload(item, depth=depth + 1) for item in value[:24]]
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:500] + "..."
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:500]


def _row_payload(store: Store, row: sqlite3.Row | None) -> dict[str, Any] | None:
    return _decode(store, row["payload"]) if row is not None else None


def _assistant_summary(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    keys = (
        "event_id", "status", "conversation_id", "thread_id", "upstream_trigger_id", "upstream_thread_key",
        "decision_id", "work_item_id", "plan_id", "execution_ids", "publication", "auto_authorized",
        "workspace_id", "error_category", "error", "created_at", "updated_at",
    )
    return _safe_payload({key: run.get(key) for key in keys if key in run})


def _execution_rows(store: Store, execution_ids: list[str]) -> list[dict[str, Any]]:
    if not execution_ids:
        return []
    placeholders = ",".join("?" for _ in execution_ids)
    rows = store.connection.execute(
        f"SELECT execution_id,work_item_id,payload,status,created_at,updated_at "
        f"FROM executions WHERE execution_id IN ({placeholders}) ORDER BY created_at",
        execution_ids,
    ).fetchall()
    return [{
        "execution_id": row["execution_id"],
        "work_item_id": row["work_item_id"],
        "status": row["status"],
        "label": _EXECUTION_LABELS.get(row["status"], row["status"]),
        "workspace_id": _row_payload(store, row).get("workspace_id"),
        "capability": _row_payload(store, row).get("capability"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    } for row in rows]


def _outbox_rows_for_event(store: Store, event_id: str) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        "SELECT outbox_id,payload,status,created_at FROM outbox "
        "WHERE idempotency_key IN (?, ?) ORDER BY created_at",
        (f"qq-publication:{event_id}", f"qq-processing-failure:{event_id}"),
    ).fetchall()
    return [{
        "outbox_id": row["outbox_id"],
        "status": row["status"],
        "body": _row_payload(store, row).get("body", ""),
        "destination": _safe_payload(_row_payload(store, row).get("destination", {})),
        "last_error": _row_payload(store, row).get("last_error"),
        "created_at": row["created_at"],
    } for row in rows]


def _audit_for_ids(store: Store, ids: list[tuple[str, str]]) -> list[dict[str, Any]]:
    if not ids:
        return []
    clauses = " OR ".join("(entity_type = ? AND entity_id = ?)" for _ in ids)
    parameters = [value for pair in ids for value in pair]
    rows = store.connection.execute(
        f"SELECT audit_id,entity_type,entity_id,action,payload,created_at FROM audit "
        f"WHERE {clauses} ORDER BY audit_id",
        parameters,
    ).fetchall()
    return [{
        "audit_id": row["audit_id"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "action": row["action"],
        "label": _AUDIT_LABELS.get((row["entity_type"], row["action"]), row["action"]),
        "details": _safe_payload(_decode(store, row["payload"])),
        "created_at": row["created_at"],
    } for row in rows]


def _current_state(run: dict[str, Any] | None, executions: list[dict[str, Any]],
                   outbox: list[dict[str, Any]]) -> tuple[str, str]:
    if any(item["status"] == "failed" for item in outbox):
        return "failed", "回复发送失败"
    if any(item["status"] == "sending" for item in outbox):
        return "sending", "正在发送回复"
    if any(item["status"] == "pending" for item in outbox):
        return "reply_pending", "等待发送回复"
    if outbox and all(item["status"] == "sent" for item in outbox):
        return "completed", "回复已发送"
    if executions:
        latest = executions[-1]
        status = latest["status"]
        return f"dsh_{status}", latest["label"]
    if run:
        status = str(run.get("status", "unknown"))
        return status, _RUN_LABELS.get(status, status)
    return "received", "已收到，等待处理"


def trace_for_event(store: Store, event_id: str) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT event_id,payload,created_at FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row is None:
        return None
    event = _decode(store, row["payload"])
    run_row = store.connection.execute(
        "SELECT payload,status,created_at,updated_at FROM assistant_runs WHERE event_id = ?", (event_id,)
    ).fetchone()
    run = _row_payload(store, run_row)
    if run is not None:
        run["status"] = run_row["status"]
        run["created_at"] = run_row["created_at"]
        run["updated_at"] = run_row["updated_at"]
    work_item_id = run.get("work_item_id") if run else None
    work_row = store.connection.execute(
        "SELECT work_item_id,payload,status,created_at,updated_at FROM work_items WHERE work_item_id = ?",
        (work_item_id,),
    ).fetchone() if work_item_id else None
    work = _row_payload(store, work_row)
    if work is not None:
        work.update({"work_item_id": work_row["work_item_id"], "status": work_row["status"],
                     "created_at": work_row["created_at"], "updated_at": work_row["updated_at"]})
    execution_ids = list(run.get("execution_ids", [])) if run else []
    executions = _execution_rows(store, execution_ids)
    outbox = _outbox_rows_for_event(store, event_id)
    ids = [("event", event_id), ("assistant_run", event_id)]
    if work_item_id:
        ids.append(("work_item", work_item_id))
    ids.extend(("execution", item["execution_id"]) for item in executions)
    ids.extend(("outbox", item["outbox_id"]) for item in outbox)
    timeline = _audit_for_ids(store, ids)
    status, status_label = _current_state(run, executions, outbox)
    error = (run or {}).get("error") or next((item.get("last_error") for item in outbox if item.get("last_error")), None)
    return {
        "event_id": event_id,
        "trace_id": event_id,
        "conversation_id": event.get("conversation_id", ""),
        "source_type": event.get("source_type", ""),
        "source_id": event.get("source_id", ""),
        "actor_id": event.get("actor_id", ""),
        "external_actor_id": event.get("external_actor_id"),
        "content": event.get("content", ""),
        "attachments": _safe_payload(event.get("attachments", [])),
        "occurred_at": event.get("occurred_at") or row["created_at"],
        "received_at": row["created_at"],
        "status": status,
        "status_label": status_label,
        "error": error,
        "assistant_run": _assistant_summary(run),
        "work_item": _safe_payload(work),
        "executions": executions,
        "outbox": outbox,
        "timeline": timeline,
    }


def _conversation_label(source_type: str, source_id: str) -> str:
    if source_type == "qq.group":
        return f"QQ 群聊 · {source_id}"
    if source_type == "qq.c2c":
        return f"QQ 私聊 · {source_id}"
    return source_type or source_id or "未命名会话"


def list_conversations(store: Store, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        "SELECT event_id FROM events ORDER BY created_at DESC LIMIT ?", (max(1, min(limit * 10, 500)),)
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        trace = trace_for_event(store, row["event_id"])
        if trace is None:
            continue
        conversation_id = trace["conversation_id"]
        group = grouped.setdefault(conversation_id, {
            "conversation_id": conversation_id,
            "label": _conversation_label(trace["source_type"], trace["source_id"]),
            "source_type": trace["source_type"],
            "source_id": trace["source_id"],
            "last_activity_at": trace["occurred_at"],
            "messages": [],
        })
        group["messages"].append({
            "id": trace["event_id"],
            "kind": "user",
            "actor_id": trace["actor_id"],
            "content": trace["content"],
            "attachments": trace["attachments"],
            "occurred_at": trace["occurred_at"],
            "status": trace["status"],
            "status_label": trace["status_label"],
            "event_id": trace["event_id"],
        })
        for reply in trace["outbox"]:
            group["messages"].append({
                "id": reply["outbox_id"],
                "kind": "agent",
                "actor_id": "company-agent",
                "content": reply["body"],
                "attachments": [],
                "occurred_at": reply["created_at"],
                "status": reply["status"],
                "status_label": _AUDIT_LABELS.get(("outbox", reply["status"]), reply["status"]),
                "event_id": trace["event_id"],
                "outbox_id": reply["outbox_id"],
            })
        group["last_activity_at"] = max(group["last_activity_at"], *(item["occurred_at"] for item in group["messages"]))
    conversations = list(grouped.values())
    for group in conversations:
        group["messages"].sort(key=lambda item: item["occurred_at"])
        group["messages"] = group["messages"][-100:]
        group["latest_status"] = next((item["status_label"] for item in reversed(group["messages"])
                                        if item["kind"] == "user"), "")
    conversations.sort(key=lambda item: item["last_activity_at"], reverse=True)
    return conversations[:limit]
