from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import Store


@dataclass(frozen=True)
class ManagedUser:
    user_id: str
    display_name: str
    time_zone: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class IdentityBinding:
    binding_id: str
    provider: str
    account_id: str
    scope_id: str
    external_id: str
    user_id: str
    created_at: str


class IdentityRegistry:
    """Resolve platform-scoped identifiers to stable internal users."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def create_user(self, display_name: str, *, time_zone: str = "UTC", user_id: str | None = None) -> ManagedUser:
        if not isinstance(display_name, str) or not isinstance(time_zone, str):
            raise ValueError("display_name and time_zone must be strings")
        name = display_name.strip()
        if not name or len(name) > 120:
            raise ValueError("display_name must contain 1 to 120 characters")
        _validate_time_zone(time_zone)
        identifier = user_id or f"user-{uuid4().hex}"
        now = _now()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO managed_users(user_id,display_name,time_zone,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (identifier, name, time_zone, "active", now, now),
            )
            _audit(connection, "managed_user", identifier, "created", {"display_name": name}, now)
        return ManagedUser(identifier, name, time_zone, "active", now, now)

    def list_users(self) -> list[ManagedUser]:
        rows = self.store.connection.execute(
            "SELECT user_id,display_name,time_zone,status,created_at,updated_at FROM managed_users ORDER BY created_at"
        ).fetchall()
        return [ManagedUser(**dict(row)) for row in rows]

    def list_bindings(self) -> list[IdentityBinding]:
        rows = self.store.connection.execute(
            "SELECT binding_id,provider,account_id,scope_id,external_id,user_id,created_at "
            "FROM identity_bindings ORDER BY created_at"
        ).fetchall()
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
