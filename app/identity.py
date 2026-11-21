from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import Store

USER_ROLES = {"admin", "employee", "customer"}
GROUP_TIERS = {"internal", "client_group"}
COMPAT_MODES = {"inherit", "on", "off"}
_BIND_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass(frozen=True)
class ManagedUser:
    user_id: str
    display_name: str
    time_zone: str
    status: str
    created_at: str
    updated_at: str
    role: str = "customer"


@dataclass(frozen=True)
class IdentityBinding:
    binding_id: str
    provider: str
    account_id: str
    scope_id: str
    external_id: str
    user_id: str
    created_at: str


@dataclass(frozen=True)
class ChatGroup:
    scope_key: str
    source_type: str
    source_id: str
    display_name: str
    tier: str
    downward_compat: str
    notes: str
    discovered_at: str
    updated_at: str


class IdentityRegistry:
    """Resolve platform-scoped identifiers to stable internal users."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def create_user(self, display_name: str, *, time_zone: str = "UTC", user_id: str | None = None,
                    role: str = "customer") -> ManagedUser:
        if not isinstance(display_name, str) or not isinstance(time_zone, str):
            raise ValueError("display_name and time_zone must be strings")
        name = display_name.strip()
        if not name or len(name) > 120:
            raise ValueError("display_name must contain 1 to 120 characters")
        normalized_role = (role or "customer").strip().lower() if isinstance(role, str) else ""
        if normalized_role not in USER_ROLES:
            raise ValueError("role must be admin, employee, or customer")
        _validate_time_zone(time_zone)
        identifier = user_id or f"user-{uuid4().hex}"
        now = _now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO managed_users(user_id,display_name,time_zone,status,role,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (identifier, name, time_zone, "active", normalized_role, now, now),
            )
            _audit(connection, "managed_user", identifier, "created",
                   {"display_name": name, "role": normalized_role}, now)
        return ManagedUser(identifier, name, time_zone, "active", now, now, role=normalized_role)

    def get_user(self, user_id: str) -> ManagedUser | None:
        row = self.store.fetchone(
            "SELECT user_id,display_name,time_zone,status,role,created_at,updated_at "
            "FROM managed_users WHERE user_id=?",
            (user_id,),
        )
        return ManagedUser(**dict(row)) if row is not None else None

    def set_user_role(self, user_id: str, role: str) -> ManagedUser:
        normalized_role = (role or "").strip().lower() if isinstance(role, str) else ""
        if normalized_role not in USER_ROLES:
            raise ValueError("role must be admin, employee, or customer")
        now = _now()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT user_id,display_name,time_zone,status,created_at FROM managed_users WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"user does not exist: {user_id}")
            connection.execute(
                "UPDATE managed_users SET role=?,updated_at=? WHERE user_id=?",
                (normalized_role, now, user_id),
            )
            _audit(connection, "managed_user", user_id, "role_updated", {"role": normalized_role}, now)
            return ManagedUser(
                user_id=row["user_id"],
                display_name=row["display_name"],
                time_zone=row["time_zone"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=now,
                role=normalized_role,
            )

    def list_users(self) -> list[ManagedUser]:
        rows = self.store.fetchall(
            "SELECT user_id,display_name,time_zone,status,role,created_at,updated_at "
            "FROM managed_users ORDER BY created_at"
        )
        return [ManagedUser(**dict(row)) for row in rows]

    def effective_admins(self, env_admin_user_ids: frozenset[str] = frozenset()) -> frozenset[str]:
        rows = self.store.fetchall(
            "SELECT user_id FROM managed_users WHERE role='admin' AND status='active'"
        )
        return env_admin_user_ids | frozenset(row[0] for row in rows)

    def user_role(self, user_id: str, env_admin_user_ids: frozenset[str] = frozenset()) -> str:
        if user_id in env_admin_user_ids:
            return "admin"
        user = self.get_user(user_id)
        if user is None or user.status != "active":
            return "customer"
        return user.role if user.role in USER_ROLES else "customer"

    def is_admin(self, user_id: str, env_admin_user_ids: frozenset[str] = frozenset()) -> bool:
        return self.user_role(user_id, env_admin_user_ids) == "admin"

    def is_internal_user(self, user_id: str, env_admin_user_ids: frozenset[str] = frozenset()) -> bool:
        return self.user_role(user_id, env_admin_user_ids) in {"admin", "employee"}

    @staticmethod
    def _group_scope_key(source_type: str, source_id: str) -> str:
        return f"{source_type.strip()}:{source_id.strip()}"

    def register_group_seen(self, source_type: str, source_id: str, *, display_name: str = "") -> ChatGroup:
        cleaned_type = (source_type or "").strip()
        cleaned_id = (source_id or "").strip()
        if not cleaned_type or not cleaned_id:
            raise ValueError("source_type and source_id are required")
        scope_key = self._group_scope_key(cleaned_type, cleaned_id)
        cleaned_name = (display_name or "").strip()[:120]
        now = _now()
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT scope_key,source_type,source_id,display_name,tier,downward_compat,notes,"
                "discovered_at,updated_at FROM chat_groups WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO chat_groups(scope_key,source_type,source_id,display_name,tier,"
                    "downward_compat,notes,discovered_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (scope_key, cleaned_type, cleaned_id, cleaned_name, "client_group", "inherit", "", now, now),
                )
                _audit(connection, "chat_group", scope_key, "discovered",
                       {"tier": "client_group", "display_name": cleaned_name}, now)
                return ChatGroup(scope_key, cleaned_type, cleaned_id, cleaned_name,
                                 "client_group", "inherit", "", now, now)
            if cleaned_name and not existing["display_name"]:
                connection.execute(
                    "UPDATE chat_groups SET display_name=?,updated_at=? WHERE scope_key=?",
                    (cleaned_name, now, scope_key),
                )
                return ChatGroup(
                    existing["scope_key"], existing["source_type"], existing["source_id"],
                    cleaned_name, existing["tier"], existing["downward_compat"],
                    existing["notes"], existing["discovered_at"], now,
                )
            return ChatGroup(**dict(existing))

    def get_group(self, source_type: str, source_id: str) -> ChatGroup | None:
        scope_key = self._group_scope_key(source_type, source_id)
        row = self.store.fetchone(
            "SELECT scope_key,source_type,source_id,display_name,tier,downward_compat,notes,"
            "discovered_at,updated_at FROM chat_groups WHERE scope_key=?",
            (scope_key,),
        )
        return ChatGroup(**dict(row)) if row is not None else None

    def list_groups(self) -> list[ChatGroup]:
        rows = self.store.fetchall(
            "SELECT scope_key,source_type,source_id,display_name,tier,downward_compat,notes,"
            "discovered_at,updated_at FROM chat_groups ORDER BY discovered_at DESC"
        )
        return [ChatGroup(**dict(row)) for row in rows]

    def update_group(self, *, source_type: str = "qq.group", source_id: str,
                     tier: str | None = None, downward_compat: str | None = None,
                     display_name: str | None = None, notes: str | None = None) -> ChatGroup:
        cleaned_type = (source_type or "qq.group").strip()
        cleaned_id = (source_id or "").strip()
        if not cleaned_type or not cleaned_id or len(cleaned_id) > 200:
            raise ValueError("source_id must contain 1 to 200 characters")
        if tier is not None and tier not in GROUP_TIERS:
            raise ValueError("tier must be internal or client_group")
        if downward_compat is not None and downward_compat not in COMPAT_MODES:
            raise ValueError("downward_compat must be inherit, on, or off")
        scope_key = self._group_scope_key(cleaned_type, cleaned_id)
        now = _now()
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT scope_key,source_type,source_id,display_name,tier,downward_compat,notes,"
                "discovered_at,updated_at FROM chat_groups WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            next_tier = tier if tier is not None else (existing["tier"] if existing else "client_group")
            next_compat = (downward_compat if downward_compat is not None
                           else (existing["downward_compat"] if existing else "inherit"))
            next_name = (display_name.strip()[:120] if display_name is not None
                         else (existing["display_name"] if existing else ""))
            next_notes = (notes.strip()[:400] if notes is not None
                          else (existing["notes"] if existing else ""))
            discovered_at = existing["discovered_at"] if existing else now
            connection.execute(
                "INSERT INTO chat_groups(scope_key,source_type,source_id,display_name,tier,"
                "downward_compat,notes,discovered_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope_key) DO UPDATE SET display_name=excluded.display_name,"
                "tier=excluded.tier,downward_compat=excluded.downward_compat,"
                "notes=excluded.notes,updated_at=excluded.updated_at",
                (scope_key, cleaned_type, cleaned_id, next_name, next_tier, next_compat,
                 next_notes, discovered_at, now),
            )
            _audit(connection, "chat_group", scope_key, "updated",
                   {"tier": next_tier, "downward_compat": next_compat, "display_name": next_name}, now)
            return ChatGroup(scope_key, cleaned_type, cleaned_id, next_name,
                             next_tier, next_compat, next_notes, discovered_at, now)

    @staticmethod
    def _normalize_bind_code(code: str) -> str:
        return "".join(ch for ch in (code or "").upper() if ch.isalnum())

    @classmethod
    def _hash_bind_code(cls, normalized: str) -> str:
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def create_bind_code(self, user_id: str, *, ttl_seconds: int = 86_400) -> dict[str, Any]:
        if not 60 <= int(ttl_seconds) <= 7 * 86_400:
            raise ValueError("ttl_seconds must be between 60 and 604800")
        user = self.get_user(user_id)
        if user is None or user.status != "active":
            raise ValueError("active user does not exist")
        raw = "".join(secrets.choice(_BIND_CODE_ALPHABET) for _ in range(8))
        display_code = f"{raw[:4]}-{raw[4:]}"
        code_hash = self._hash_bind_code(raw)
        created = datetime.now(timezone.utc)
        expires = (created + timedelta(seconds=int(ttl_seconds))).isoformat()
        now = created.isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO identity_bind_codes(code_hash,user_id,role_hint,expires_at,created_at) "
                "VALUES (?,?,?,?,?)",
                (code_hash, user_id, user.role, expires, now),
            )
            _audit(connection, "managed_user", user_id, "bind_code_created",
                   {"role": user.role, "expires_at": expires}, now)
        return {"code": display_code, "user_id": user.user_id, "display_name": user.display_name,
                "role": user.role, "expires_at": expires}

    def redeem_bind_code(self, code: str, *, provider: str, account_id: str, external_id: str,
                         scope_id: str = "*") -> dict[str, Any]:
        normalized = self._normalize_bind_code(code)
        if len(normalized) < 6 or len(normalized) > 32:
            raise ValueError("invalid bind code format")
        cleaned = [provider.strip(), account_id.strip(), (scope_id or "*").strip() or "*", external_id.strip()]
        if any(not item or len(item) > 200 for item in cleaned):
            raise ValueError("binding fields must contain 1 to 200 characters")
        code_hash = self._hash_bind_code(normalized)
        now = _now()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT code_hash,user_id,role_hint,expires_at,used_at FROM identity_bind_codes WHERE code_hash=?",
                (code_hash,),
            ).fetchone()
            if row is None:
                raise ValueError("bind code is invalid")
            if row["used_at"] is not None:
                raise ValueError("bind code has already been used")
            if row["expires_at"] <= now:
                raise ValueError("bind code has expired")
            user_row = connection.execute(
                "SELECT user_id,display_name,time_zone,status,role,created_at,updated_at "
                "FROM managed_users WHERE user_id=? AND status='active'",
                (row["user_id"],),
            ).fetchone()
            if user_row is None:
                raise ValueError("target user is no longer active")
            binding_id = f"binding-{uuid4().hex}"
            connection.execute(
                "INSERT INTO identity_bindings(binding_id,provider,account_id,scope_id,external_id,user_id,created_at) "
                "VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(provider,account_id,scope_id,external_id) DO UPDATE SET user_id=excluded.user_id",
                (binding_id, cleaned[0], cleaned[1], cleaned[2], cleaned[3], user_row["user_id"], now),
            )
            connection.execute(
                "UPDATE identity_bind_codes SET used_at=?,used_by_external_id=? WHERE code_hash=?",
                (now, cleaned[3], code_hash),
            )
            _audit(connection, "managed_user", user_row["user_id"], "bind_code_redeemed",
                   {"provider": cleaned[0], "scope_id": cleaned[2], "role": user_row["role"]}, now)
            return {
                "user_id": user_row["user_id"],
                "display_name": user_row["display_name"],
                "role": user_row["role"],
                "scope_id": cleaned[2],
            }

    def list_bindings(self) -> list[IdentityBinding]:
        rows = self.store.fetchall(
            "SELECT binding_id,provider,account_id,scope_id,external_id,user_id,created_at "
            "FROM identity_bindings ORDER BY created_at"
        )
        return [IdentityBinding(**dict(row)) for row in rows]

    def bind(self, *, provider: str, account_id: str, scope_id: str, external_id: str,
             user_id: str) -> IdentityBinding:
        if any(not isinstance(value, str) for value in (provider, account_id, scope_id, external_id, user_id)):
            raise ValueError("identity binding fields must be strings")
        values = [provider.strip(), account_id.strip(), scope_id.strip() or "*", external_id.strip(), user_id.strip()]
        if any(not value or len(value) > 200 for value in values):
            raise ValueError("identity binding fields must contain 1 to 200 characters")
        now = _now()
        binding = IdentityBinding(f"binding-{uuid4().hex}", *values[:4], values[4], now)
        with self.store.transaction() as connection:
            if not connection.execute("SELECT 1 FROM managed_users WHERE user_id=? AND status='active'", (values[4],)).fetchone():
                raise ValueError("active user does not exist")
            connection.execute(
                "INSERT INTO identity_bindings(binding_id,provider,account_id,scope_id,external_id,user_id,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (binding.binding_id, binding.provider, binding.account_id, binding.scope_id,
                 binding.external_id, binding.user_id, now),
            )
            _audit(connection, "identity_binding", binding.binding_id, "bound", asdict(binding), now)
        return binding

    def resolve(self, *, provider: str, account_id: str, scope_id: str, external_id: str) -> str:
        with self.store._transaction_lock:
            row = self.store.connection.execute(
                "SELECT user_id FROM identity_bindings WHERE provider=? AND account_id=? AND scope_id=? AND external_id=?",
                (provider, account_id, scope_id, external_id),
            ).fetchone()
            if row is None:
                row = self.store.connection.execute(
                    "SELECT user_id FROM identity_bindings WHERE provider=? AND account_id=? AND scope_id='*' AND external_id=?",
                    (provider, account_id, external_id),
                ).fetchone()
            return row[0] if row else f"external:{provider}:{account_id}:{scope_id}:{external_id}"

    def unbind(self, binding_id: str) -> bool:
        now = _now()
        with self.store.transaction() as connection:
            row = connection.execute("SELECT 1 FROM identity_bindings WHERE binding_id=?", (binding_id,)).fetchone()
            if row is None:
                return False
            connection.execute("DELETE FROM identity_bindings WHERE binding_id=?", (binding_id,))
            _audit(connection, "identity_binding", binding_id, "unbound", {}, now)
        return True


def _validate_time_zone(value: str) -> None:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, TypeError) as error:
        raise ValueError("time_zone must be a valid IANA time zone") from error


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(connection, entity_type: str, entity_id: str, action: str, payload: dict, now: str) -> None:
    import json
    connection.execute(
        "INSERT INTO audit(entity_type,entity_id,action,payload,created_at) VALUES (?,?,?,?,?)",
        (entity_type, entity_id, action, json.dumps(payload, ensure_ascii=False), now),
    )
