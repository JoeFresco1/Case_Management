"""System configuration settings (T7 diagnostics + system utilities / P10).

This module owns the **tenant-scoped, versioned, audited** system settings
behind the ``System Configuration`` right (wired as
:class:`Action.ADMIN_TENANT_CONFIG`, the tenant-config admin action owned by
the ``process_admin`` role). It covers the four T7 surfaces from
``specs/002-leftovers/spec.md:177``:

- **module log settings** — per-module level/rotation/forwarding
  (``logging.<module>`` keys; ELK/Splunk sinks are adapter config only,
  never live connections in this slice).
- **cache/service diagnostics config** — ``diagnostics.cache`` (bounds for
  the diagnostics cache) and ``diagnostics.services`` (desired state of the
  background services surfaced by the diagnostics endpoint).
- **storage volume/cache abstraction** — ``storage.volume`` declares which
  volume the tenant wants (``local-fs`` today, ``azure-blob`` reserved).
  The declaration is config data; the *choice* of adapter is enforced in
  infrastructure (``app.infrastructure.system.local_system_adapters``),
  never in domain logic.
- **encryption-at-rest adapter config** — ``encryption.at_rest`` declares
  the at-rest posture (``enabled`` + ``key_ref``). The local slice stores
  bytes in plaintext and fails closed when ``enabled`` is requested
  without the KeyVault seam (see the infrastructure adapter).

# Why this lives in application, not domain

Settings cross aggregates (logging policy + storage declaration + audit
history) and are wired through Protocol ports (clock, store) the domain
does not own. The orchestration belongs here; validation is pure and
unit-testable with in-memory fakes.

# Versioning + audit

Every key carries a monotonically increasing ``version`` starting at 1.
Updates require ``expected_version`` (optimistic concurrency, the T7-US1
concurrent-edit protection): a stale token raises :class:`Conflict`
(HTTP 409) instead of silently overwriting. Every version is retained in
the store history with actor + timestamp + change summary, and every
mutation returns an :class:`SettingAuditPayload` whose value is already
secret-redacted — the durable record behind the admin history view.

# Secret redaction

All outward payloads (reads, mutation responses, audit payloads) pass
through :func:`redact_sensitive` (FM-011). A setting value that embeds a
secret-like key (``password``, ``token``, ...) is stored verbatim but
never rendered verbatim. ``encryption.at_rest.key_ref`` is a *reference
id* (Constitution V — sensitive values referenced by id, not content)
and intentionally survives redaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from app.application.telemetry.redaction import redact_sensitive
from app.domain.errors import Conflict, NotFound, ValidationFailed

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Modules that accept a per-module log setting. Closed set so a typo'd
#: module fails at validation time (422) instead of silently doing nothing.
LOGGING_MODULES: Final[frozenset[str]] = frozenset(
    {
        "cases",
        "workflow",
        "documents",
        "audit",
        "search",
        "tasks",
        "telemetry",
    }
)

#: Allowed per-module levels (mirrors ``Settings.log_level`` values).
LOG_LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR")

#: Log forwarding sinks. Adapter names only — this slice never opens a
#: network connection; ELK/Splunk forwarding is a future adapter behind
#: the telemetry seam (``docs/integrations/seams.md`` seam 6).
LOG_SINKS: Final[tuple[str, ...]] = ("none", "elk", "splunk")

#: Rotation policies for the local log files.
LOG_ROTATIONS: Final[tuple[str, ...]] = ("hourly", "daily", "size-100mb")

#: Storage adapters a tenant may *declare*. ``azure-blob`` is accepted as
#: declared config but the local infrastructure adapter refuses to serve
#: it (fail-closed, ``DependencyUnavailable``) until the Azure seam lands.
STORAGE_ADAPTERS: Final[tuple[str, ...]] = ("local-fs", "azure-blob")

#: Storage tiers for the volume abstraction (platter/cache/volume
#: equivalent in business terminology: hot cache vs cold archive).
STORAGE_TIERS: Final[tuple[str, ...]] = ("standard", "archive")

#: Background services whose desired state is configurable and whose
#: actual state is reported by the diagnostics endpoint.
DIAGNOSTIC_SERVICES: Final[tuple[str, ...]] = (
    "outbox",
    "sla_scanner",
    "webhook_dispatcher",
)

#: Audit event type emitted for every setting mutation.
AUDIT_EVENT_TYPE: Final[str] = "SystemSettingUpdated"


def _default_value(key: str) -> dict[str, object]:
    """Return the default value for a known setting key.

    Why a function, not a module dict: callers mutate the returned dict
    when seeding a store; a shared module-level dict would leak mutations
    across tenants and tests.
    """
    if key.startswith("logging."):
        return {"level": "INFO", "sink": "none", "rotation": "daily"}
    if key == "diagnostics.cache":
        return {"max_entries": 1000, "ttl_seconds": 300}
    if key == "diagnostics.services":
        return {"outbox": True, "sla_scanner": True, "webhook_dispatcher": True}
    if key == "storage.volume":
        return {"adapter": "local-fs", "cache_mb": 256, "tier": "standard"}
    if key == "encryption.at_rest":
        return {"enabled": False, "key_ref": ""}
    raise ValidationFailed(f"unknown system setting {key!r}", field="key")


def _validate_logging_value(key: str, value: dict[str, object]) -> None:
    """Validate a ``logging.<module>`` value; raise ValidationFailed."""
    module = key[len("logging.") :]
    if module not in LOGGING_MODULES:
        raise ValidationFailed(
            f"unknown log module {module!r}; expected one of {sorted(LOGGING_MODULES)}",
            field="key",
        )
    level = value.get("level")
    if level not in LOG_LEVELS:
        raise ValidationFailed(
            f"invalid log level {level!r}; expected one of {list(LOG_LEVELS)}",
            field="level",
        )
    sink = value.get("sink")
    if sink not in LOG_SINKS:
        raise ValidationFailed(
            f"invalid log sink {sink!r}; expected one of {list(LOG_SINKS)}",
            field="sink",
        )
    rotation = value.get("rotation")
    if rotation not in LOG_ROTATIONS:
        raise ValidationFailed(
            f"invalid rotation {rotation!r}; expected one of {list(LOG_ROTATIONS)}",
            field="rotation",
        )


def _validate_cache_value(value: dict[str, object]) -> None:
    """Validate a ``diagnostics.cache`` value; raise ValidationFailed."""
    max_entries = value.get("max_entries")
    if not isinstance(max_entries, int) or not 1 <= max_entries <= 100_000:
        raise ValidationFailed("max_entries must be an int in 1..100000", field="max_entries")
    ttl = value.get("ttl_seconds")
    if not isinstance(ttl, int) or not 0 <= ttl <= 86_400:
        raise ValidationFailed("ttl_seconds must be an int in 0..86400", field="ttl_seconds")


def _validate_services_value(value: dict[str, object]) -> None:
    """Validate a ``diagnostics.services`` value; raise ValidationFailed."""
    for service in DIAGNOSTIC_SERVICES:
        flag = value.get(service)
        if not isinstance(flag, bool):
            raise ValidationFailed(f"service flag {service!r} must be a bool", field=service)


def _validate_volume_value(value: dict[str, object]) -> None:
    """Validate a ``storage.volume`` value; raise ValidationFailed."""
    adapter = value.get("adapter")
    if adapter not in STORAGE_ADAPTERS:
        raise ValidationFailed(
            f"invalid storage adapter {adapter!r}; expected one of {list(STORAGE_ADAPTERS)}",
            field="adapter",
        )
    cache_mb = value.get("cache_mb")
    if not isinstance(cache_mb, int) or not 1 <= cache_mb <= 65_536:
        raise ValidationFailed("cache_mb must be an int in 1..65536", field="cache_mb")
    tier = value.get("tier")
    if tier not in STORAGE_TIERS:
        raise ValidationFailed(
            f"invalid storage tier {tier!r}; expected one of {list(STORAGE_TIERS)}",
            field="tier",
        )


def _validate_encryption_value(value: dict[str, object]) -> None:
    """Validate an ``encryption.at_rest`` value; raise ValidationFailed."""
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise ValidationFailed("enabled must be a bool", field="enabled")
    key_ref = value.get("key_ref")
    if not isinstance(key_ref, str) or len(key_ref) > 256:
        raise ValidationFailed("key_ref must be a string of at most 256 chars", field="key_ref")
    if enabled and not key_ref.strip():
        # why: without a key reference there is nothing for the
        # future KeyVault adapter to resolve; accepting
        # enabled-without-key would promise encryption we cannot
        # deliver (fail-closed, Constitution V).
        raise ValidationFailed(
            "key_ref is required when encryption at rest is enabled", field="key_ref"
        )


def _validate_value(key: str, value: dict[str, object]) -> None:
    """Validate a candidate value for ``key``; raise ValidationFailed."""
    if key.startswith("logging."):
        _validate_logging_value(key, value)
    elif key == "diagnostics.cache":
        _validate_cache_value(value)
    elif key == "diagnostics.services":
        _validate_services_value(value)
    elif key == "storage.volume":
        _validate_volume_value(value)
    elif key == "encryption.at_rest":
        _validate_encryption_value(value)
    else:
        raise ValidationFailed(f"unknown system setting {key!r}", field="key")


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SystemSetting:
    """One versioned system-setting row (latest version per key).

    Why frozen: versions are append-only; an update creates a *new*
    :class:`SystemSetting` with ``version + 1`` instead of mutating.
    """

    tenant_id: UUID
    key: str
    value: dict[str, object]
    version: int
    updated_by: UUID
    updated_at: datetime
    change_summary: str


@dataclass(frozen=True, slots=True)
class SettingAuditPayload:
    """Redacted audit record for one setting mutation.

    The value is already passed through :func:`redact_sensitive` so the
    payload is safe for logs, audit rows, and outbox events (no
    secrets/PHI ever land in the audit trail, Constitution V).
    """

    event_type: str
    tenant_id: UUID
    key: str
    version: int
    actor_id: UUID
    occurred_at: datetime
    redacted_value: dict[str, object]
    change_summary: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class UpdateSettingCommand:
    """Mutate one system setting with optimistic concurrency."""

    tenant_id: UUID
    key: str
    value: dict[str, object]
    expected_version: int
    actor_id: UUID
    change_summary: str = ""
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class UpdateSettingResult:
    """Mutation outcome: the new version plus its redacted audit payload."""

    setting: SystemSetting
    audit: SettingAuditPayload


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


class SystemSettingsStore(Protocol):
    """Tenant-scoped versioned store (implemented in-memory here; SQL later)."""

    async def latest(self, tenant_id: UUID, key: str) -> SystemSetting | None: ...

    async def list_latest(self, tenant_id: UUID) -> list[SystemSetting]: ...

    async def history(self, tenant_id: UUID, key: str) -> list[SystemSetting]: ...

    async def append(self, setting: SystemSetting) -> None: ...


class _Clock(Protocol):
    def now(self) -> datetime: ...


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


def known_keys() -> list[str]:
    """Return every settable key (defaults exist for each)."""
    return [f"logging.{module}" for module in sorted(LOGGING_MODULES)] + [
        "diagnostics.cache",
        "diagnostics.services",
        "storage.volume",
        "encryption.at_rest",
    ]


async def ensure_defaults(
    tenant_id: UUID, *, store: SystemSettingsStore, actor_id: UUID, clock: _Clock
) -> list[SystemSetting]:
    """Seed version-1 defaults for every key missing in this tenant.

    Idempotent: keys that already have a version are left untouched, so
    re-running seed after an admin customized a value never clobbers it.
    """
    now = clock.now()
    seeded: list[SystemSetting] = []
    for key in known_keys():
        if await store.latest(tenant_id, key) is not None:
            continue
        setting = SystemSetting(
            tenant_id=tenant_id,
            key=key,
            value=_default_value(key),
            version=1,
            updated_by=actor_id,
            updated_at=now,
            change_summary="seeded default",
        )
        await store.append(setting)
        seeded.append(setting)
    return seeded


async def list_settings(tenant_id: UUID, *, store: SystemSettingsStore) -> list[SystemSetting]:
    """Return the latest version of every setting, redacted for display.

    The stored value is never mutated; redaction applies to the *copy*
    handed to callers (reads must not leak secrets even to admins
    without a need-to-know, FM-011).
    """
    settings = await store.list_latest(tenant_id)
    return [
        SystemSetting(
            tenant_id=s.tenant_id,
            key=s.key,
            value=dict(redact_sensitive(s.value)),
            version=s.version,
            updated_by=s.updated_by,
            updated_at=s.updated_at,
            change_summary=s.change_summary,
        )
        for s in sorted(settings, key=lambda s: s.key)
    ]


async def get_setting(tenant_id: UUID, key: str, *, store: SystemSettingsStore) -> SystemSetting:
    """Return one setting (redacted copy); unknown key raises NotFound."""
    setting = await store.latest(tenant_id, key)
    if setting is None:
        raise NotFound(f"unknown system setting {key!r}")
    return SystemSetting(
        tenant_id=setting.tenant_id,
        key=setting.key,
        value=dict(redact_sensitive(setting.value)),
        version=setting.version,
        updated_by=setting.updated_by,
        updated_at=setting.updated_at,
        change_summary=setting.change_summary,
    )


async def update_setting(
    cmd: UpdateSettingCommand, *, store: SystemSettingsStore, clock: _Clock
) -> UpdateSettingResult:
    """Validate and append a new version of ``cmd.key``.

    Raises:
        ValidationFailed: unknown key or malformed value.
        Conflict: ``expected_version`` is stale (concurrent-edit
            protection, T7-US1 — the caller must refetch and retry).
        NotFound: the key has no version yet (call ``ensure_defaults``
            first or seed via the router, which seeds on read).
    """
    _validate_value(cmd.key, cmd.value)
    current = await store.latest(cmd.tenant_id, cmd.key)
    if current is None:
        raise NotFound(f"unknown system setting {cmd.key!r}; seed defaults first")
    if current.version != cmd.expected_version:
        raise Conflict(
            f"stale version for {cmd.key!r}: expected {cmd.expected_version}, "
            f"current is {current.version}"
        )
    now = clock.now()
    # why: copy the value so later caller-side mutation cannot rewrite
    # history (versions are append-only; the stored dict must be stable).
    stored_value = dict(cmd.value)
    setting = SystemSetting(
        tenant_id=cmd.tenant_id,
        key=cmd.key,
        value=stored_value,
        version=current.version + 1,
        updated_by=cmd.actor_id,
        updated_at=now,
        change_summary=cmd.change_summary,
    )
    await store.append(setting)
    audit = SettingAuditPayload(
        event_type=AUDIT_EVENT_TYPE,
        tenant_id=cmd.tenant_id,
        key=cmd.key,
        version=setting.version,
        actor_id=cmd.actor_id,
        occurred_at=now,
        redacted_value=dict(redact_sensitive(stored_value)),
        change_summary=cmd.change_summary,
        correlation_id=cmd.correlation_id,
    )
    return UpdateSettingResult(setting=setting, audit=audit)


# ---------------------------------------------------------------------------
# In-memory store (local/test adapter; SQL later behind the same Protocol)
# ---------------------------------------------------------------------------


class InMemorySystemSettingsStore:
    """Tenant-isolated versioned store for local/test (no I/O, no Azure).

    History per ``(tenant_id, key)`` is a list; ``latest`` returns the
    last entry. Cross-tenant reads return ``None``/``[]`` (never the
    other tenant's rows), so tenant-isolation tests assert on absence.
    """

    def __init__(self) -> None:
        self._versions: dict[tuple[UUID, str], list[SystemSetting]] = {}
        # why: insertion order is append order per key; the dataclass
        # field below is intentionally absent — plain dict init keeps
        # construction allocation-free for the request-scoped fallback.
        self._seeded: set[UUID] = set()

    async def latest(self, tenant_id: UUID, key: str) -> SystemSetting | None:
        """Return the newest version, or None (tenant-isolated)."""
        versions = self._versions.get((tenant_id, key))
        if not versions:
            return None
        return versions[-1]

    async def list_latest(self, tenant_id: UUID) -> list[SystemSetting]:
        """Return the newest version of every key for this tenant only."""
        out: list[SystemSetting] = []
        for (tenant, _key), versions in self._versions.items():
            if tenant == tenant_id and versions:
                out.append(versions[-1])
        return out

    async def history(self, tenant_id: UUID, key: str) -> list[SystemSetting]:
        """Return every version of one key, oldest-first (audit trail)."""
        return list(self._versions.get((tenant_id, key), []))

    async def append(self, setting: SystemSetting) -> None:
        """Append a new version (caller owns version increment + validation)."""
        # why: defensive copy — the store must not alias the caller's
        # dict, otherwise a later caller-side mutation rewrites history.
        frozen = SystemSetting(
            tenant_id=setting.tenant_id,
            key=setting.key,
            value=dict(setting.value),
            version=setting.version,
            updated_by=setting.updated_by,
            updated_at=setting.updated_at,
            change_summary=setting.change_summary,
        )
        self._versions.setdefault((setting.tenant_id, setting.key), []).append(frozen)

    def seeded_tenants(self) -> set[UUID]:
        """Tenants already seeded (lets routers skip redundant seeding)."""
        return set(self._seeded)

    def mark_seeded(self, tenant_id: UUID) -> None:
        """Record that defaults were seeded for ``tenant_id``."""
        self._seeded.add(tenant_id)


__all__ = [
    "AUDIT_EVENT_TYPE",
    "DIAGNOSTIC_SERVICES",
    "LOGGING_MODULES",
    "LOG_LEVELS",
    "LOG_ROTATIONS",
    "LOG_SINKS",
    "STORAGE_ADAPTERS",
    "STORAGE_TIERS",
    "InMemorySystemSettingsStore",
    "SettingAuditPayload",
    "SystemSetting",
    "SystemSettingsStore",
    "UpdateSettingCommand",
    "UpdateSettingResult",
    "ensure_defaults",
    "get_setting",
    "known_keys",
    "list_settings",
    "update_setting",
]
