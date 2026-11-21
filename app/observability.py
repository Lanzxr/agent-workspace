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
    ("managed_user", "created"): "创建用户",
    ("managed_user", "role_updated"): "修改用户角色",
    ("managed_user", "bind_code_created"): "生成身份绑定码",
    ("managed_user", "bind_code_redeemed"): "通过绑定码完成绑定",
    ("identity_binding", "bound"): "绑定平台身份",
    ("identity_binding", "unbound"): "解除身份绑定",
    ("chat_group", "discovered"): "自动发现新群聊 (默认 L1)",
    ("chat_group", "updated"): "更新群聊层级/配置",
    ("knowledge_share", "shared"): "添加跨区显式授权分享",
    ("knowledge_share", "unshared"): "撤销跨区分享",
    ("setting", "web_search_enabled"): "切换 DSH 联网开关",
    ("setting", "dssuperagent_web_search_enabled"): "切换 DSS 联网开关",
    ("setting", "downward_compatibility_enabled"): "切换向下兼容开关",
    ("setting", "ambient_listen_enabled"): "切换群聊旁听模式开关",
    ("setting", "ambient_cooldown_seconds"): "调整群聊遇冷等待时间",
    ("setting", "bot_aliases"): "更新机器人称呼别名",
    ("capability_gap", "recorded"): "记录群聊能力提升需求",
    ("capability_gap", "resolved"): "标记能力缺口已补齐",
    ("capability_gap", "dismissed"): "忽略能力缺口记录",
    ("capability_gap", "open"): "重新打开能力缺口",
}


def audit_label(entity_type: str, action: str, entity_id: str = "") -> str:
    if (entity_type, entity_id) in _AUDIT_LABELS:
        return _AUDIT_LABELS[(entity_type, entity_id)]
    return _AUDIT_LABELS.get((entity_type, action), action)


def _epoch(value: Any) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.timestamp()
        return dt.timestamp()
    except ValueError:
        return 0.0


def _load_identity_context(store: Store) -> dict[str, Any]:
    with store._transaction_lock:
        users: dict[str, dict[str, str]] = {}
        for row in store.connection.execute("SELECT user_id,display_name,role,status FROM managed_users"):
            users[row["user_id"]] = {
                "display_name": row["display_name"],
                "role": row["role"],
                "status": row["status"],
            }
        bindings: dict[tuple[str, str, str], str] = {}
        for row in store.connection.execute("SELECT provider,account_id,scope_id,external_id,user_id FROM identity_bindings"):
            bindings[(row["scope_id"], row["external_id"], row["provider"])] = row["user_id"]
        groups: dict[str, dict[str, str]] = {}
        for row in store.connection.execute("SELECT scope_key,display_name,tier,downward_compat,notes FROM chat_groups"):
            groups[row["scope_key"]] = {
                "display_name": row["display_name"],
                "tier": row["tier"],
                "downward_compat": row["downward_compat"],
                "notes": row["notes"],
            }
        return {"users": users, "bindings": bindings, "groups": groups}


def _resolve_actor_meta(ctx: dict[str, Any], *, actor_id: str, external_actor_id: str | None,
                        source_type: str, source_id: str) -> tuple[str, str]:
    users = ctx["users"]
    if actor_id in users:
        info = users[actor_id]
        return info["display_name"], info["role"]
    ext = external_actor_id or ""
    if not ext and actor_id.startswith("external:"):
        parts = actor_id.split(":", 4)
        if len(parts) == 5:
            ext = parts[4]
    if ext:
        bindings = ctx["bindings"]
        scope = source_id if source_type == "qq.group" else "*"
        uid = bindings.get((scope, ext, "qq")) or bindings.get(("*", ext, "qq"))
        if uid and uid in users:
            info = users[uid]
            return info["display_name"], info["role"]
        return f"未绑定 ({ext[:8]}…)" if len(ext) > 10 else f"未绑定 ({ext})", "customer"
    return actor_id or "未知用户", "customer"


def _resolve_scene_meta(ctx: dict[str, Any], *, source_type: str, source_id: str,
                        actor_role: str) -> dict[str, str]:
    if source_type in {"qq.group", "qq.guild"}:
        grp = ctx["groups"].get(f"{source_type}:{source_id}", {})
        tier = grp.get("tier") or "client_group"
        compat = grp.get("downward_compat") or "inherit"
        display_name = grp.get("display_name") or ""
        tier_label = "内部群 L2" if tier == "internal" else "客户群 L1"
        return {
            "group_display_name": display_name,
            "group_tier": tier,
            "downward_compat": compat,
            "tier_label": tier_label,
        }
    if source_type in {"qq.c2c", "qq.direct", "qq.private"}:
        if actor_role == "admin":
            tier_label, tier = "管理员私聊 L3", "admin"
        elif actor_role == "employee":
            tier_label, tier = "员工私聊 L2", "internal"
        else:
            tier_label, tier = "客户私聊 L1", "direct"
        return {
            "group_display_name": "",
            "group_tier": tier,
            "downward_compat": "inherit",
            "tier_label": tier_label,
        }
    return {"group_display_name": "", "group_tier": "internal", "downward_compat": "inherit", "tier_label": source_type or "内部"}


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
    rows = store.fetchall(
        f"SELECT execution_id,work_item_id,payload,status,created_at,updated_at "
        f"FROM executions WHERE execution_id IN ({placeholders}) ORDER BY created_at",
        execution_ids,
    )
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
    rows = store.fetchall(
        "SELECT outbox_id,idempotency_key,payload,status,created_at FROM outbox "
        "WHERE idempotency_key IN (?, ?, ?) ORDER BY created_at",
        (f"qq-ack:{event_id}", f"qq-publication:{event_id}", f"qq-processing-failure:{event_id}"),
    )
    return [{
        "outbox_id": row["outbox_id"],
        "kind": "ack" if str(row["idempotency_key"]).startswith("qq-ack:") else "reply",
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
    rows = store.fetchall(
        f"SELECT audit_id,entity_type,entity_id,action,payload,created_at FROM audit "
        f"WHERE {clauses} ORDER BY audit_id",
        parameters,
    )
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
    final_outbox = [item for item in outbox if item.get("kind") != "ack"]
    if any(item["status"] == "failed" for item in final_outbox):
        return "failed", "回复发送失败"
    if any(item["status"] == "sending" for item in final_outbox):
        return "sending", "正在发送回复"
    if any(item["status"] == "pending" for item in final_outbox):
        return "reply_pending", "等待发送回复"
    if final_outbox and all(item["status"] == "sent" for item in final_outbox):
        return "completed", "回复已发送"
    if executions:
        latest = executions[-1]
        status = latest["status"]
        return f"dsh_{status}", latest["label"]
    if run:
        status = str(run.get("status", "unknown"))
        return status, _RUN_LABELS.get(status, status)
    return "received", "已收到，等待处理"


def trace_for_event(store: Store, event_id: str, *, _ctx: dict[str, Any] | None = None) -> dict[str, Any] | None:
    row = store.fetchone(
        "SELECT event_id,payload,created_at FROM events WHERE event_id = ?", (event_id,)
    )
    if row is None:
        return None
    ctx = _ctx or _load_identity_context(store)
    event = _decode(store, row["payload"])
    run_row = store.fetchone(
        "SELECT payload,status,created_at,updated_at FROM assistant_runs WHERE event_id = ?", (event_id,)
    )
    run = _row_payload(store, run_row)
    if run is not None:
        run["status"] = run_row["status"]
        run["created_at"] = run_row["created_at"]
        run["updated_at"] = run_row["updated_at"]
    work_item_id = run.get("work_item_id") if run else None
    work_row = store.fetchone(
        "SELECT work_item_id,payload,status,created_at,updated_at FROM work_items WHERE work_item_id = ?",
        (work_item_id,),
    ) if work_item_id else None
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
    source_type = event.get("source_type", "")
    source_id = event.get("source_id", "")
    actor_id = event.get("actor_id", "")
    external_actor_id = event.get("external_actor_id")
    actor_display_name, actor_role = _resolve_actor_meta(
        ctx, actor_id=actor_id, external_actor_id=external_actor_id,
        source_type=source_type, source_id=source_id,
    )
    scene = _resolve_scene_meta(ctx, source_type=source_type, source_id=source_id, actor_role=actor_role)
    workspace_id = next((e.get("workspace_id") for e in reversed(executions) if e.get("workspace_id")), None) or (
        (run or {}).get("workspace_id")
    )
    return {
        "event_id": event_id,
        "trace_id": event_id,
        "conversation_id": event.get("conversation_id", ""),
        "source_type": source_type,
        "source_id": source_id,
        "actor_id": actor_id,
        "actor_display_name": actor_display_name,
        "actor_role": actor_role,
        "external_actor_id": external_actor_id,
        "workspace_id": workspace_id,
        **scene,
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


def _conversation_label(source_type: str, source_id: str, *, group_name: str = "", actor_name: str = "") -> str:
    if source_type == "qq.group":
        return group_name or f"QQ 群 · {source_id[:12]}"
    if source_type == "qq.c2c":
        if actor_name and not actor_name.startswith("未绑定"):
            return f"QQ 私聊 · {actor_name}"
        return f"QQ 私聊 · {source_id[:12]}"
    return group_name or source_type or source_id or "未命名会话"


def list_conversations(store: Store, *, limit: int = 50) -> list[dict[str, Any]]:
    ctx = _load_identity_context(store)
    rows = store.fetchall(
        "SELECT event_id FROM events ORDER BY created_at DESC LIMIT ?", (max(1, min(limit * 10, 500)),)
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        trace = trace_for_event(store, row["event_id"], _ctx=ctx)
        if trace is None:
            continue
        conversation_id = trace["conversation_id"]
        label = _conversation_label(
            trace["source_type"], trace["source_id"],
            group_name=trace["group_display_name"],
            actor_name=trace["actor_display_name"],
        )
        group = grouped.setdefault(conversation_id, {
            "conversation_id": conversation_id,
            "label": label,
            "source_type": trace["source_type"],
            "source_id": trace["source_id"],
            "group_tier": trace["group_tier"],
            "downward_compat": trace["downward_compat"],
            "tier_label": trace["tier_label"],
            "workspace_id": trace["workspace_id"],
            "last_activity_at": trace["occurred_at"],
            "messages": [],
        })
        if label and not group["label"].endswith(label) and not trace["actor_display_name"].startswith("未绑定") and trace["source_type"] == "qq.c2c":
            group["label"] = label
        if trace["workspace_id"] and not group["workspace_id"]:
            group["workspace_id"] = trace["workspace_id"]
        group["messages"].append({
            "id": trace["event_id"],
            "kind": "user",
            "actor_id": trace["actor_id"],
            "actor_display_name": trace["actor_display_name"],
            "actor_role": trace["actor_role"],
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
                "actor_display_name": "Company Agent",
                "actor_role": "system",
                "content": reply["body"],
                "attachments": [],
                "occurred_at": reply["created_at"],
                "status": reply["status"],
                "status_label": _AUDIT_LABELS.get(("outbox", reply["status"]), reply["status"]),
                "event_id": trace["event_id"],
                "outbox_id": reply["outbox_id"],
            })
        group["last_activity_at"] = max(
            [group["last_activity_at"], *(item["occurred_at"] for item in group["messages"])],
            key=_epoch,
        )
    conversations = list(grouped.values())
    for group in conversations:
        group["messages"].sort(key=lambda item: (_epoch(item["occurred_at"]), 0 if item["kind"] == "user" else 1))
        group["messages"] = group["messages"][-100:]
        group["latest_status"] = next((item["status_label"] for item in reversed(group["messages"])
                                        if item["kind"] == "user"), "")
    conversations.sort(key=lambda item: _epoch(item["last_activity_at"]), reverse=True)
    return conversations[:limit]
