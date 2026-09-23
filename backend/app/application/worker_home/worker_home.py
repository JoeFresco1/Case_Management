"""Worker-home use cases (T5 worker UX): dashboard, preferences, admin policy.

# Why this module exists

The T5 worker-UX paragraph requires a configurable home/dashboard
(favorites, shortcuts, grouping, task pane, recents, import preview) plus
user preferences under an admin-enforced preference policy where the
admin value **overrides** the user value.

# Auth model

- Dashboard + preference reads/writes are per-user; any authenticated
  actor may manage their own record (cross-user ids raise ``Forbidden``,
  cross-tenant ids surface as ``NotFound`` so existence never leaks).
- Policy reads are per-tenant (any authenticated actor); policy writes
  require ``ADMIN_TENANT_CONFIG`` (process_admin), deny-by-default.

# Failure semantics

- Auth / validation errors raise before any write.
- Every mutation appends an audit row + enqueues an outbox event in the
  caller's transaction; the router commits once (FR-L02).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.context import UserContext
from app.domain.errors import Forbidden, NotFound
from app.domain.ports import AuditEventStore, OutboxWriter
from app.domain.worker_home import (
    DashboardConfig,
    EffectivePreference,
    PreferencePolicy,
    UserPreferences,
    resolve_effective_preferences,
)

DASHBOARD_SAVED = "WorkerDashboardSaved"
PREFERENCES_SAVED = "WorkerPreferencesSaved"
PREFERENCE_POLICY_PUBLISHED = "WorkerPreferencePolicyPublished"


class DashboardStore(Protocol):
    """Process-local dashboard persistence (tenant + user scoped)."""

    async def get(self, *, tenant_id: UUID, user_id: UUID) -> DashboardConfig | None: ...

    async def put(self, *, config: DashboardConfig) -> None: ...


class PreferenceStore(Protocol):
    """Process-local preference persistence (tenant + user scoped)."""

    async def get(self, *, tenant_id: UUID, user_id: UUID) -> UserPreferences | None: ...

    async def put(self, *, preferences: UserPreferences) -> None: ...


class PreferencePolicyStore(Protocol):
    """Process-local admin-policy persistence (tenant scoped)."""

    async def get(self, *, tenant_id: UUID) -> PreferencePolicy | None: ...

    async def put(self, *, policy: PreferencePolicy) -> None: ...


class InMemoryDashboardStore:
    """In-memory dashboard store for local/test (keyed by tenant + user)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, UUID], DashboardConfig] = {}

    async def get(self, *, tenant_id: UUID, user_id: UUID) -> DashboardConfig | None:
        """Return the stored config iff it belongs to the same tenant."""
        row = self._rows.get((tenant_id, user_id))
        if row is not None and row.tenant_id != tenant_id:
            return None
        return row

    async def put(self, *, config: DashboardConfig) -> None:
        """Upsert one dashboard config (tenant + user key)."""
        self._rows[(config.tenant_id, config.user_id)] = config


class InMemoryPreferenceStore:
    """In-memory preference store for local/test (keyed by tenant + user)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, UUID], UserPreferences] = {}

    async def get(self, *, tenant_id: UUID, user_id: UUID) -> UserPreferences | None:
        """Return stored preferences iff they belong to the same tenant."""
        row = self._rows.get((tenant_id, user_id))
        if row is not None and row.tenant_id != tenant_id:
            return None
        return row

    async def put(self, *, preferences: UserPreferences) -> None:
        """Upsert one preference record (tenant + user key)."""
        self._rows[(preferences.tenant_id, preferences.user_id)] = preferences


class InMemoryPreferencePolicyStore:
    """In-memory admin-policy store for local/test (keyed by tenant)."""

    def __init__(self) -> None:
        self._rows: dict[UUID, PreferencePolicy] = {}

    async def get(self, *, tenant_id: UUID) -> PreferencePolicy | None:
        """Return the tenant policy iff it belongs to the same tenant."""
        row = self._rows.get(tenant_id)
        if row is not None and row.tenant_id != tenant_id:
            return None
        return row

    async def put(self, *, policy: PreferencePolicy) -> None:
        """Upsert one tenant policy."""
        self._rows[policy.tenant_id] = policy


def _actor(*, tenant_id: UUID, user_id: UUID, roles: tuple[str, ...]) -> UserContext:
    """Build the policy subject without leaking display names into authz."""
    return UserContext(user_id=user_id, tenant_id=tenant_id, display_name="", roles=roles)


def _new_id() -> UUID:
    """Return a fresh event id for audit/outbox correlation."""
    return uuid4()


async def _append_audit_and_outbox(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    subject_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    occurred_at: datetime,
    audit: AuditEventStore,
    outbox: OutboxWriter,
) -> None:
    """Append one audit row + enqueue the matching outbox event."""
    correlation_id = str(_new_id())
    await audit.append(
        tenant_id=tenant_id,
        event_type=event_type,
        actor_id=actor_id,
        actor_type="user",
        subject_type="worker_home",
        subject_id=subject_id,
        case_id=None,
        correlation_id=correlation_id,
        causation_id=None,
        payload=payload,
        occurred_at=occurred_at,
    )
    await outbox.enqueue(
        tenant_id=tenant_id,
        event_type=event_type,
        event_id=uuid4(),
        payload={"subject_id": str(subject_id), **payload},
    )


@dataclass(frozen=True, slots=True)
class SaveDashboardCommand:
    """Validated intent to replace the actor's home dashboard config."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    target_user_id: UUID
    widgets: tuple[str, ...] = ()
    favorites: tuple[UUID, ...] = ()
    shortcuts: tuple[tuple[str, str], ...] = ()
    grouping: str = "none"
    sort: str = "recent"
    task_pane_case_id: UUID | None = None


async def save_dashboard(
    command: SaveDashboardCommand,
    *,
    dashboards: DashboardStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> DashboardConfig:
    """Replace the actor's dashboard config (own record only, audited).

    Raises:
        Forbidden: when ``target_user_id`` is not the actor (workers
            cannot rewrite each other's home layout).
    """
    if command.target_user_id != command.actor_id:
        raise Forbidden("cannot modify another user's dashboard")
    config = DashboardConfig.create(
        user_id=command.actor_id,
        tenant_id=command.tenant_id,
        widgets=command.widgets,
        favorites=command.favorites,
        shortcuts=command.shortcuts,
        grouping=command.grouping,
        sort=command.sort,
        task_pane_case_id=command.task_pane_case_id,
    )
    now = clock.now()
    await dashboards.put(config=config)
    await _append_audit_and_outbox(
        tenant_id=command.tenant_id,
        actor_id=command.actor_id,
        subject_id=command.actor_id,
        event_type=DASHBOARD_SAVED,
        payload={"widgets": list(config.widgets), "favorites": len(config.favorites)},
        occurred_at=now,
        audit=audit,
        outbox=outbox,
    )
    return config


@dataclass(frozen=True, slots=True)
class SavePreferencesCommand:
    """Validated intent to replace the actor's preference values."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    target_user_id: UUID
    values: dict[str, str]


async def save_preferences(
    command: SavePreferencesCommand,
    *,
    preferences: PreferenceStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> UserPreferences:
    """Replace the actor's preferences (own record only, audited).

    Raises:
        Forbidden: when ``target_user_id`` is not the actor.
    """
    if command.target_user_id != command.actor_id:
        raise Forbidden("cannot modify another user's preferences")
    record = UserPreferences.create(
        user_id=command.actor_id, tenant_id=command.tenant_id, values=dict(command.values)
    )
    await preferences.put(preferences=record)
    await _append_audit_and_outbox(
        tenant_id=command.tenant_id,
        actor_id=command.actor_id,
        subject_id=command.actor_id,
        event_type=PREFERENCES_SAVED,
        payload={"keys": sorted(record.values.keys())},
        occurred_at=clock.now(),
        audit=audit,
        outbox=outbox,
    )
    return record


@dataclass(frozen=True, slots=True)
class PublishPolicyCommand:
    """Admin intent to publish the tenant preference policy."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    locked: dict[str, str]
    allowed: dict[str, list[str]]


async def publish_policy(
    command: PublishPolicyCommand,
    *,
    policies: PreferencePolicyStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> PreferencePolicy:
    """Publish the tenant policy (``ADMIN_TENANT_CONFIG`` only, audited)."""
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _actor(
            tenant_id=command.tenant_id,
            user_id=command.actor_id,
            roles=command.actor_roles,
        ),
        Resource(tenant_id=command.tenant_id, kind="tenant_policy"),
    )
    policy = PreferencePolicy.create(
        tenant_id=command.tenant_id,
        locked=dict(command.locked),
        allowed={key: list(values) for key, values in command.allowed.items()},
        updated_by=command.actor_id,
    )
    await policies.put(policy=policy)
    await _append_audit_and_outbox(
        tenant_id=command.tenant_id,
        actor_id=command.actor_id,
        subject_id=command.tenant_id,
        event_type=PREFERENCE_POLICY_PUBLISHED,
        payload={"locked": sorted(policy.locked.keys())},
        occurred_at=clock.now(),
        audit=audit,
        outbox=outbox,
    )
    return policy


async def effective_preferences(
    *,
    tenant_id: UUID,
    user_id: UUID,
    preferences: PreferenceStore,
    policies: PreferencePolicyStore,
) -> tuple[EffectivePreference, ...]:
    """Return the merged effective preferences (admin policy wins).

    Missing user rows resolve to defaults; missing policy rows mean no
    enforcement. Cross-tenant rows are treated as missing (``NotFound``
    semantics stay at the router; the merge itself never leaks).
    """
    stored = await preferences.get(tenant_id=tenant_id, user_id=user_id)
    if stored is None or stored.tenant_id != tenant_id:
        stored = UserPreferences(user_id=user_id, tenant_id=tenant_id, values={})
    policy = await policies.get(tenant_id=tenant_id)
    if policy is not None and policy.tenant_id != tenant_id:
        policy = None
    return resolve_effective_preferences(user=stored, policy=policy)


async def get_dashboard(
    *, tenant_id: UUID, user_id: UUID, dashboards: DashboardStore
) -> DashboardConfig | None:
    """Return the user's dashboard config, tenant-scoped (``None`` = unset)."""
    row = await dashboards.get(tenant_id=tenant_id, user_id=user_id)
    if row is not None and row.tenant_id != tenant_id:
        raise NotFound("dashboard not found")
    return row


__all__ = [
    "DASHBOARD_SAVED",
    "PREFERENCES_SAVED",
    "PREFERENCE_POLICY_PUBLISHED",
    "DashboardStore",
    "InMemoryDashboardStore",
    "InMemoryPreferencePolicyStore",
    "InMemoryPreferenceStore",
    "PreferencePolicyStore",
    "PreferenceStore",
    "PublishPolicyCommand",
    "SaveDashboardCommand",
    "SavePreferencesCommand",
    "effective_preferences",
    "get_dashboard",
    "publish_policy",
    "save_dashboard",
    "save_preferences",
]
