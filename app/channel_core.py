from __future__ import annotations

import hashlib
import inspect
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .attachments import AttachmentIngestor
from .control_plane import ControlPlane, InvalidTransition, NotFound
from .dssuperagent import DssuperAgentError
from .identity import IdentityRegistry
from .knowledge.service import KnowledgeService
from .models import (
    DIRECT_SOURCE_TYPES,
    GROUP_SOURCE_TYPES,
    IM_SOURCE_TYPES,
    Event,
    OutboxMessage,
    is_direct_source,
    is_group_source,
    is_im_source,
    source_identity_scope,
    source_provider,
)
from .reply_contract import extract_final_reply
from .tool_forge import ForgeSecretsVault, ToolForgeError, ToolForgeService
from .workspace_retention import resolve_workspace_file, touch_workspace_files

DEFAULT_BOT_ALIASES = ("憨憨", "机器人", "小助手", "AI助手", "助手")
DEFAULT_AMBIENT_COOLDOWN_SECONDS = 300.0


def truncate_utf8_text(text: str, max_bytes: int) -> str:
    """Truncate text cleanly at a UTF-8 character boundary within max_bytes."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


class BaseChannelService:
    """Shared IM channel orchestration for slash commands, isolation tiers, triage, DSH dispatch, and outbox delivery."""

    channel_name: str = ""
    provider_name: str = ""
    channel_display_name: str = ""
    policy_prefix: str = ""
    max_reply_chars: int = 1800
    max_reply_bytes: int | None = None

    def __init__(
        self,
        plane: ControlPlane,
        workflow: Any,
        settings: Any,
        *,
        identities: IdentityRegistry | None = None,
        attachment_ingestor: AttachmentIngestor | None = None,
        knowledge_service: KnowledgeService | None = None,
        workspace_manager: Any | None = None,
        admin_user_ids: frozenset[str] | None = None,
        tool_forge: ToolForgeService | None = None,
        forge_secrets_vault: ForgeSecretsVault | None = None,
    ) -> None:
        self.plane = plane
        self.workflow = workflow
        self.settings = settings
        self.identities = identities or IdentityRegistry(plane.store)
        self.knowledge_service = knowledge_service
        self.workspace_manager = workspace_manager
        self.admin_user_ids = admin_user_ids
        self.tool_forge = tool_forge
        self.forge_secrets_vault = forge_secrets_vault
        self._ephemeral_secrets_lock = threading.Lock()
        self._ephemeral_forge_secrets: dict[str, tuple[str, str]] = {}
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

    def _account_id(self) -> str:
        for attr in ("account_id", "app_id", "bot_id"):
            val = getattr(self.settings, attr, "")
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    def _fit_reply_body(self, body: str) -> str:
        if self.max_reply_bytes is not None:
            return truncate_utf8_text(body, self.max_reply_bytes)
        return body[:self.max_reply_chars]

    def _event_explicitly_mentions_bot(self, event: Event, destination: dict[str, str]) -> bool:
        return destination.get("mentions_bot") == "1"

    def _deliver_outbox_message(
        self,
        message: OutboxMessage,
        *,
        file_path: Path | None = None,
        filename: str | None = None,
    ) -> None:
        raise NotImplementedError

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

    def _persist_prepared_event(
        self,
        event: Event,
        destination: dict[str, str],
        *,
        group_display_name: str = "",
    ) -> tuple[Event, dict[str, str]] | None:
        raw_content = event.content.strip()
        secret_pair: tuple[str, str] | None = None
        if raw_content.lower().startswith("/forge-secret"):
            sec_parts = raw_content.split(None, 3)
            if len(sec_parts) >= 4 and sec_parts[1].lower() == "set":
                secret_pair = (sec_parts[2], sec_parts[3])
                redacted = f"/forge-secret set {sec_parts[2]} [REDACTED]"
                event = Event(
                    event.event_id, event.source_type, event.source_id, event.conversation_id,
                    event.actor_id, event.occurred_at, redacted, event.attachments,
                    event.capabilities, external_actor_id=event.external_actor_id,
                )
        external_actor_id = event.actor_id
        try:
            saved_event = self.plane.get_event(event.event_id)
        except NotFound:
            saved_event = None
        if saved_event is None and secret_pair is not None:
            with self._ephemeral_secrets_lock:
                self._ephemeral_forge_secrets[event.event_id] = secret_pair
        if saved_event is not None:
            if (saved_event.source_type != event.source_type or saved_event.source_id != event.source_id
                    or saved_event.conversation_id != event.conversation_id
                    or saved_event.occurred_at != event.occurred_at
                    or not saved_event.content.startswith(event.content)
                    or (saved_event.external_actor_id is not None
                        and saved_event.external_actor_id != external_actor_id)):
                print(
                    f"company-agent: discarded conflicting duplicate {self.channel_display_name} event",
                    file=sys.stderr,
                )
                return None
            event = Event(saved_event.event_id, saved_event.source_type, saved_event.source_id,
                          saved_event.conversation_id, saved_event.actor_id, saved_event.occurred_at,
                          saved_event.content, saved_event.attachments, saved_event.capabilities,
                          external_actor_id=saved_event.external_actor_id)
        else:
            identity_scope = source_identity_scope(event.source_type, event.source_id)
            event = Event(
                event.event_id,
                event.source_type,
                event.source_id,
                event.conversation_id,
                self.identities.resolve(
                    provider=self.provider_name,
                    account_id=self._account_id(),
                    scope_id=identity_scope,
                    external_id=external_actor_id,
                ),
                event.occurred_at,
                event.content,
                event.attachments,
                event.capabilities,
                external_actor_id=external_actor_id,
            )
        if is_group_source(event.source_type):
            self.identities.register_group_seen(
                event.source_type, event.source_id, display_name=group_display_name,
            )
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
            print(
                f"company-agent: {self.channel_display_name} event processing failed ({type(error).__name__})",
                file=sys.stderr,
            )
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
            prev_id = prev.get("event_id")
            ob = self.plane.store.fetchone(
                "SELECT payload, created_at FROM outbox WHERE idempotency_key IN (?, ?) AND status IN ('sent','sending','pending')",
                (f"{self.policy_prefix}-publication:{prev_id}", f"qq-publication:{prev_id}"),
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
        if is_direct_source(event.source_type):
            return "direct"
        if self._event_explicitly_mentions_bot(event, destination):
            return "direct"
        text = event.content.strip()
        if not text:
            return "chatter"

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
            print(
                f"company-agent: DSSuperAgent classify unavailable ({type(error).__name__}); deferring safely",
                file=sys.stderr,
            )
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
        if is_group_source(event.source_type):
            group = self.identities.get_group(event.source_type, event.source_id)
            digest = hashlib.sha256(f"{event.source_type}:{event.source_id}".encode("utf-8")).hexdigest()[:12]
            if group is not None and group.tier == "internal":
                workspace_id, tier = f"ws-internal-{digest}", "internal"
            else:
                workspace_id, tier = f"ws-client-{digest}", "client_group"
        elif is_direct_source(event.source_type):
            role = self.identities.user_role(event.actor_id, self._env_admin_ids())
            prefix = "dm" if self.provider_name == "qq" else f"{self.provider_name}-dm"
            digest = hashlib.sha256(f"{prefix}:{event.conversation_id}".encode("utf-8")).hexdigest()[:12]
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

    def _is_admin_dm(self, event: Event) -> bool:
        return is_direct_source(event.source_type) and self.identities.is_admin(
            event.actor_id, self._env_admin_ids()
        )

    def _get_tool_forge(self) -> ToolForgeService | None:
        if self.tool_forge is not None:
            return self.tool_forge
        if self.forge_secrets_vault is not None:
            self.tool_forge = ToolForgeService(
                self.plane,
                secrets_vault=self.forge_secrets_vault,
                shared_tools=getattr(self.workflow, "shared_tools", None),
            )
            return self.tool_forge
        if self.workspace_manager is not None and hasattr(self.workspace_manager, "base_directory"):
            data_dir = Path(self.workspace_manager.base_directory).parent
            self.tool_forge = ToolForgeService(
                self.plane,
                secrets_vault=ForgeSecretsVault(data_dir / "forge-secrets.json"),
                shared_tools=getattr(self.workflow, "shared_tools", None),
            )
            return self.tool_forge
        return None

    def _handle_forge_command(self, event: Event, cmd: str, arg: str) -> str:
        # Always pop ephemeral plaintext immediately so it never stays in memory on rejection or error.
        ephemeral = None
        with self._ephemeral_secrets_lock:
            ephemeral = self._ephemeral_forge_secrets.pop(event.event_id, None)
        if cmd == "/forge-secret" and ephemeral is None:
            parts_raw = arg.split(None, 2)
            if len(parts_raw) == 3 and parts_raw[0].lower() == "set":
                ephemeral = (parts_raw[1], parts_raw[2])
                try:
                    self.plane.redact_event_content(event.event_id, f"/forge-secret set {parts_raw[1]} [REDACTED]")
                except (NotFound, ValueError):
                    pass

        if not is_direct_source(event.source_type):
            return (
                f"安全隔离限制：Tool Forge 域名/凭据管理与工具上线审批仅允许管理员在本人的 {self.channel_display_name} 私聊窗口执行，"
                "群聊内一律禁止。"
            )
        if not self.identities.is_admin(event.actor_id, self._env_admin_ids()):
            return "权限不足：仅企业管理员 (L3) 可操作 Tool Forge。"
        forge = self._get_tool_forge()
        if forge is None:
            return "当前服务未启用 Tool Forge 运行时。"
        actor = f"{self.provider_name}:{event.actor_id}"

        try:
            if cmd in {"/forge", "/forge-help"}:
                return (
                    "Tool Forge 管理员私聊指令：\n"
                    "- /forge-domain list | add <域名> [--subdomains] | del <域名>\n"
                    "- /forge-secret list | set <ALIAS> <SECRET> | del <ALIAS>\n"
                    "- /forge-proposals （查看待审批发布提案）\n"
                    "- /forge-approve <proposal_id> | /forge-reject <proposal_id> [原因]\n"
                    "- /forge-tools | /forge-enable <tool_id> | /forge-disable <tool_id> | /forge-rollback <tool_id> [vN]"
                )
            if cmd == "/forge-domain":
                tokens = arg.split()
                sub = tokens[0].lower() if tokens else "list"
                if sub == "list":
                    domains = forge.list_domains()
                    if not domains:
                        return "当前联网域名白名单为空（所有自定义工具外网请求都会被拦截）。添加示例：/forge-domain add api.example.com"
                    lines = ["当前允许自定义工具访问的外部域名："]
                    for d in domains:
                        flag = "（含子域名）" if d.get("allow_subdomains") else ""
                        lines.append(f"- {d['domain']}{flag}")
                    return "\n".join(lines)
                if sub == "add" and len(tokens) >= 2:
                    allow_sub = "--subdomains" in tokens[2:] or "--sub" in tokens[2:]
                    entry = forge.add_domain(tokens[1], allow_subdomains=allow_sub, created_by=actor)
                    return f"已加入外网白名单：{entry['domain']}{'（含子域名）' if entry['allow_subdomains'] else ''}。"
                if sub in {"del", "remove"} and len(tokens) >= 2:
                    removed = forge.remove_domain(tokens[1], actor=actor)
                    return f"已移出外网白名单：{tokens[1]}。" if removed else f"白名单中未找到域名：{tokens[1]}。"
                return "用法：/forge-domain list | add <域名> [--subdomains] | del <域名>"

            if cmd == "/forge-secret":
                tokens = arg.split(None, 2)
                sub = tokens[0].lower() if tokens else "list"
                if sub == "list":
                    items = forge.list_secrets_metadata()
                    if not items:
                        return "当前凭据保险库为空。添加示例：/forge-secret set APIZENO_KEY <密钥>"
                    lines = ["已配置的外部凭据别名（仅管理员主机侧持有明文，员工群及 AI 沙箱均不可见）："]
                    for item in items:
                        lines.append(f"- {item['name']} ({item.get('preview', '***')}, length={item.get('length', 0)})")
                    return "\n".join(lines)
                if sub == "set":
                    pair = ephemeral or ((tokens[1], tokens[2]) if len(tokens) >= 3 else None)
                    if pair is None:
                        return "用法：/forge-secret set <SECRET_ALIAS> <SECRET_VALUE>"
                    meta = forge.set_secret(pair[0], pair[1], actor=actor)
                    return f"已加密保存凭据 {meta['name']}（原始私聊消息已脱敏；员工群与 DSH 沙箱均无法读取明文）。"
                if sub in {"del", "remove"} and len(tokens) >= 2:
                    ok = forge.delete_secret(tokens[1], actor=actor)
                    return f"已删除凭据：{tokens[1]}。" if ok else f"未找到凭据：{tokens[1]}。"
                return "用法：/forge-secret list | set <ALIAS> <VALUE> | del <ALIAS>"

            if cmd == "/forge-proposals":
                pending = forge.list_proposals(status="pending", limit=20)
                if not pending:
                    return "当前没有待审批的工具发布提案。"
                lines = ["待审批工具发布提案："]
                for p in pending:
                    domains = ", ".join(p.get("allowed_domains") or []) or "无外网"
                    secrets = ", ".join(p.get("secret_refs") or []) or "无密钥"
                    lines.append(
                        f"- {p['proposal_id']} | {p['tool_id']}@v{p['target_version']} ({p['kind']}, {p['operation_type']})\n"
                        f"  可见范围: {p['target_visibility']} | 域名: {domains} | 凭据: {secrets}\n"
                        f"  说明: {p['summary']}\n"
                        f"  批准指令: /forge-approve {p['proposal_id']}"
                    )
                return "\n".join(lines)

            if cmd == "/forge-approve":
                pid = arg.strip().split()[0] if arg.strip() else ""
                if not pid:
                    return "用法：/forge-approve <proposal_id>"
                res = forge.approve_proposal(pid, decided_by=actor, is_admin=True)
                target_note = "已写入 shared-tools" if res["kind"] == "shared_script" else "已热挂载至对应层级 MCP，立即可用"
                return f"已批准上线：{res['tool_id']}@v{res['active_version']} -> {res['visibility']}（{target_note}，无需重启服务）。"

            if cmd == "/forge-reject":
                parts_r = arg.strip().split(None, 1)
                if not parts_r:
                    return "用法：/forge-reject <proposal_id> [拒绝原因]"
                res = forge.reject_proposal(parts_r[0], decided_by=actor, is_admin=True, reason=parts_r[1] if len(parts_r) > 1 else "")
                return f"已驳回提案 {res['proposal_id']}（{res['tool_id']}）。"

            if cmd == "/forge-tools":
                tools = forge.list_tools()
                if not tools:
                    return "当前尚未创建任何 Tool Forge 工具。你可以在管理员私聊直接让我接入接口、试跑并生成可复用工具。"
                lines = ["Tool Forge 已登记工具："]
                for t in tools[:25]:
                    st = t.get("stats") or {}
                    status = "启用中" if (t["enabled"] and t["active_version"] > 0) else ("已停用" if t["active_version"] > 0 else "未发布草稿")
                    lines.append(
                        f"- {t['tool_id']} (active=v{t['active_version']}/latest=v{t['latest_version']}, {t['visibility']}, {status}, 调用 {st.get('ok_calls', 0)}/{st.get('calls', 0)} 均耗 {st.get('avg_ms', 0)}ms)"
                    )
                return "\n".join(lines)

            if cmd in {"/forge-enable", "/forge-disable"}:
                tid = arg.strip().split()[0] if arg.strip() else ""
                if not tid:
                    return f"用法：{cmd} <tool_id>"
                res = forge.set_tool_enabled(tid, enabled=(cmd == "/forge-enable"), actor=actor)
                return f"已{'启用' if res['enabled'] else '停用'}工具 {res['tool_id']}（active=v{res['active_version']}）。"

            if cmd == "/forge-rollback":
                parts_rb = arg.strip().split()
                if not parts_rb:
                    return "用法：/forge-rollback <tool_id> [目标版本号，如 v1]"
                target_ver = None
                if len(parts_rb) >= 2:
                    raw_v = parts_rb[1].lstrip("vV")
                    target_ver = int(raw_v)
                res = forge.rollback_tool(parts_rb[0], target_version=target_ver, actor=actor)
                return f"已回滚工具 {res['tool_id']}：v{res['previous_version']} -> v{res['active_version']}（作用域 {res['visibility']} 已立即生效）。"

            return "未知 Tool Forge 指令，请发送 /forge-help 查看完整用法。"
        except (ToolForgeError, ValueError) as error:
            return f"Tool Forge 操作失败：{error}"

    def _handle_slash_command(self, event: Event, destination: dict[str, str]) -> bool:
        raw = event.content.strip()
        if not raw:
            return False
        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        forge_commands = {
            "/forge", "/forge-help", "/forge-domain", "/forge-secret", "/forge-proposals",
            "/forge-approve", "/forge-reject", "/forge-tools", "/forge-enable",
            "/forge-disable", "/forge-rollback",
        }
        if cmd not in ({"/bind", "/mark-internal", "/mark-client", "/compat", "/whoami", "/internal", "/save-internal"} | forge_commands):
            return False
        digest = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:24]
        idempotency_key = f"{self.policy_prefix}-publication:{event.event_id}"
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
                        arg,
                        provider=self.provider_name,
                        account_id=self._account_id(),
                        external_id=external_id,
                        scope_id="*",
                    )
                except ValueError as error:
                    reply = f"绑定失败：{error}"
                else:
                    role_names = {"admin": "企业管理员 (L3)", "employee": "企业员工 (L2)", "customer": "客户 (L1)"}
                    role_label = role_names.get(bound["role"], bound["role"])
                    reply = f"绑定成功！当前账号已关联「{bound['display_name']}」，角色：{role_label}。"
        elif cmd in {"/mark-internal", "/mark-client"}:
            if not is_group_source(event.source_type):
                reply = f"请在需要设置的{self.channel_display_name}群内发送该命令。"
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
            if not is_group_source(event.source_type):
                reply = f"请在{self.channel_display_name}群内发送 /compat on | off | inherit（或在管理台设置）。"
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
            if is_group_source(event.source_type):
                group = self.identities.get_group(event.source_type, event.source_id)
                tier_desc = "企业内部群 (L2)" if (group and group.tier == "internal") else "客户隔离群 (L1，受房间天花板限制)"
                reply = f"身份：{role_names.get(role, role)}\n当前场景：{tier_desc}"
            else:
                reply = f"身份：{role_names.get(role, role)}\n当前场景：私聊"
        elif cmd in {"/internal", "/save-internal"}:
            group_locked = False
            if is_group_source(event.source_type):
                group = self.identities.get_group(event.source_type, event.source_id)
                group_locked = group is None or group.tier != "internal"
            if not self.identities.is_internal_user(event.actor_id, self._env_admin_ids()):
                reply = "权限不足：仅企业员工或管理员可将资料存入企业内部共享知识库。请先联系管理员生成绑定码并发送 /bind。"
            elif group_locked:
                reply = "安全隔离限制：客户群 (L1) 内禁止写入企业内部共享知识库。请私聊机器人或在内部群内发送 /internal。"
            elif event.attachments and not arg:
                reply = "已收到附件，将作为「企业内部共享资料」入库（内部群与员工私聊可用）。"
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
                reply = f"已存入企业内部共享知识库（{saved['filename']}），内部群与员工均可检索使用。"
        elif cmd in forge_commands:
            reply = self._handle_forge_command(event, cmd, arg)

        if reply is None:
            return False
        self._publish_command_reply(
            event,
            destination,
            body=reply,
            decision_id=f"{self.policy_prefix}-command-{digest}",
            idempotency_key=idempotency_key,
        )
        return True

    def _publish_command_reply(
        self,
        event: Event,
        destination: dict[str, str],
        *,
        body: str,
        decision_id: str,
        idempotency_key: str,
    ) -> None:
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
                    policy_version=getattr(getattr(self.workflow, "policy", None), "version", f"{self.policy_prefix}-command-v1"),
                )
        self.plane.decide_publication(
            decision_id=decision_id,
            work_item_id=None,
            conversation_id=conversation_id,
            disposition=disposition,
            reason=f"{self.channel_display_name} channel self-service command response",
            policy_version=f"{self.policy_prefix}-command-v1",
            body=self._fit_reply_body(body) if allowed else "",
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
            policy_version = getattr(getattr(self.workflow, "policy", None), "version", f"{self.policy_prefix}-fallback-v1")
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
            decision_id=f"{self.policy_prefix}-processing-failed-{digest}",
            work_item_id=None,
            conversation_id=conversation_id,
            disposition=disposition,
            reason=f"{self.channel_display_name} event processing failed ({category or 'unknown'})",
            policy_version=f"{self.policy_prefix}-processing-failure-v1",
            body="这条消息已收到，但本次处理未能完成，因此没有生成可交付的结论。请稍后重新发送请求。",
            idempotency_key=f"{self.policy_prefix}-processing-failure:{event.event_id}",
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
            policy_note = (
                f"publication fallback: private_nudge -> group_update because {self.channel_display_name} private publication is unavailable"
            )
        elif chosen == "suppress" and disposition != "suppress":
            policy_note = f"publication blocked: {disposition} is not allowlisted for this {self.channel_display_name} channel"
        return chosen, policy_note, disposition

    def _on_execution_started(self, event: Event, run: Any, item: Any) -> None:
        if not event.source_type.startswith(f"{self.provider_name}."):
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
            decision_id=f"{self.policy_prefix}-ack-{digest}",
            work_item_id=getattr(run, "work_item_id", None),
            conversation_id=getattr(run, "conversation_id", None) or event.conversation_id,
            disposition=chosen,
            reason=reason,
            policy_version=str(getattr(run, "policy_version", f"{self.policy_prefix}-channel-v1")),
            body=body,
            idempotency_key=f"{self.policy_prefix}-ack:{event.event_id}",
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
        body = self._fit_reply_body(body)
        digest = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:24]
        reason = str(recommendation.get("reason", ""))[:1000] or f"{self.channel_display_name} channel publication policy"
        if policy_note:
            reason = f"{reason}; {policy_note} (requested={requested_disposition}, chosen={chosen})"
        self.plane.decide_publication(
            decision_id=f"{self.policy_prefix}-publication-{digest}",
            work_item_id=work_item_id,
            conversation_id=run.get("conversation_id") or event.conversation_id,
            disposition=chosen,
            reason=reason,
            policy_version=str(run.get("policy_version", f"{self.policy_prefix}-channel-v1")),
            body=body,
            idempotency_key=f"{self.policy_prefix}-publication:{event.event_id}",
            destination=destination if publishable else None,
        )
        for index, file_meta in enumerate(deliverables, start=1):
            self.plane.decide_publication(
                decision_id=f"{self.policy_prefix}-file-{digest}-{index}",
                work_item_id=work_item_id,
                conversation_id=run.get("conversation_id") or event.conversation_id,
                disposition=chosen,
                reason=f"{reason} (file deliverable {index}: {file_meta['filename']})"[:1000],
                policy_version=str(run.get("policy_version", f"{self.policy_prefix}-channel-v1")),
                body=f"[文件] {file_meta['filename']}",
                idempotency_key=f"{self.policy_prefix}-file:{event.event_id}:{index}",
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

    def _transport_ready(self) -> bool:
        return True

    def flush_outbox(self, *, respect_backoff: bool = False, max_attempts: int = 12) -> None:
        if not self._transport_ready():
            return
        with self._outbox_lock:
            now_epoch = time.time()
            for pending in self.plane.list_outbox():
                if pending.status not in {"pending", "failed"} or pending.destination.get("channel") != self.channel_name:
                    continue
                if pending.publication_disposition not in self.settings.allowed_publications:
                    self.plane.cancel_outbox(
                        pending.outbox_id,
                        f"{self.channel_display_name} publication is no longer allowlisted",
                    )
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
                            raise RuntimeError(f"WorkspaceManager is unavailable for {self.channel_display_name} file delivery")
                        ws_id = str(att.get("workspace_id") or "")
                        rel_path = str(att.get("path") or "")
                        filename = str(att.get("filename") or "") or None
                        resolved = resolve_workspace_file(manager, ws_id, rel_path)
                        touch_workspace_files(manager, ws_id, [rel_path])
                        self._deliver_outbox_message(sending, file_path=resolved, filename=filename)
                    else:
                        self._deliver_outbox_message(sending)
                except Exception as error:
                    self.plane.mark_outbox_failed(sending.outbox_id, str(error))
                    print(
                        f"company-agent: {self.channel_display_name} Outbox delivery failed "
                        f"({sending.outbox_id}): {str(error)[:240]}",
                        file=sys.stderr,
                    )
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
