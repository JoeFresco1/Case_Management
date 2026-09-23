"""System-settings use-case tests (T7 diagnostics + system utilities / P10).

Acceptance encoded as ``test_<unit>_<scenario>__<expectation>``
(AGENTS.md §4.R5):

- **Versioned**: updates increment the version; history retains every
  version with actor + summary (audited).
- **Concurrent-edit protection**: stale ``expected_version`` raises
  ``Conflict`` (T7-US1, no silent overwrite).
- **Validated**: unknown keys/modules/levels/sinks/adapters raise
  ``ValidationFailed``; encryption-on without ``key_ref`` fails closed.
- **Secret redaction**: secret-like keys are stored verbatim but every
  outward surface (list/get/audit payload) renders ``[REDACTED]``.
- **Tenant isolation**: tenant B never sees tenant A's rows or versions.
- **Azure-free**: the in-memory store + local adapters need no container.

Deterministic: fixed clock, fixed UUIDs, in-memory store only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.application.configuration import system_settings as sysconf
from app.domain.errors import Conflict, NotFound, ValidationFailed

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

FIXED_NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)


class _FixedClock:
    """Deterministic clock for the use-case port."""

    def now(self) -> datetime:
        return FIXED_NOW


async def _seeded(
    store: sysconf.InMemorySystemSettingsStore | None = None,
) -> sysconf.InMemorySystemSettingsStore:
    """Seed defaults for tenant A; return the store."""
    # // given a fresh store
    store = store or sysconf.InMemorySystemSettingsStore()
    # // when seeding defaults
    seeded = await sysconf.ensure_defaults(
        TENANT_A, store=store, actor_id=ADMIN_A, clock=_FixedClock()
    )
    # // then every known key has a version-1 row
    assert {s.key for s in seeded} == set(sysconf.known_keys())
    assert all(s.version == 1 for s in seeded)
    return store


async def test_ensure_defaults__reseed_after_customize__keeps_custom_value() -> None:
    """Re-seeding never clobbers an admin-customized value."""
    # // given seeded defaults with a customized log level
    store = await _seeded()
    await sysconf.update_setting(
        sysconf.UpdateSettingCommand(
            tenant_id=TENANT_A,
            key="logging.cases",
            value={"level": "DEBUG", "sink": "none", "rotation": "daily"},
            expected_version=1,
            actor_id=ADMIN_A,
            change_summary="verbose intake triage",
        ),
        store=store,
        clock=_FixedClock(),
    )
    # // when re-seeding
    reseeded = await sysconf.ensure_defaults(
        TENANT_A, store=store, actor_id=ADMIN_A, clock=_FixedClock()
    )
    # // then nothing is re-seeded and the custom value survives
    assert reseeded == []
    current = await sysconf.get_setting(TENANT_A, "logging.cases", store=store)
    assert current.value["level"] == "DEBUG"
    assert current.version == 2


async def test_update_setting__valid_log_setting__increments_version_with_audit() -> None:
    """A valid module-log update appends version 2 plus a redacted audit payload."""
    # // given seeded defaults
    store = await _seeded()
    # // when updating the workflow log sink
    result = await sysconf.update_setting(
        sysconf.UpdateSettingCommand(
            tenant_id=TENANT_A,
            key="logging.workflow",
            value={"level": "WARNING", "sink": "elk", "rotation": "hourly"},
            expected_version=1,
            actor_id=ADMIN_A,
            change_summary="forward workflow warnings",
            correlation_id="corr-1",
        ),
        store=store,
        clock=_FixedClock(),
    )
    # // then version 2 with audit metadata and redacted payload
    assert result.setting.version == 2
    assert result.setting.updated_by == ADMIN_A
    assert result.setting.change_summary == "forward workflow warnings"
    assert result.audit.event_type == sysconf.AUDIT_EVENT_TYPE
    assert result.audit.version == 2
    assert result.audit.correlation_id == "corr-1"
    assert result.audit.redacted_value["sink"] == "elk"
    # // and history retains both versions (audited trail)
    history = await store.history(TENANT_A, "logging.workflow")
    assert [s.version for s in history] == [1, 2]


async def test_update_setting__stale_expected_version__raises_conflict() -> None:
    """A concurrent edit with a stale token raises Conflict (T7-US1)."""
    # // given version 2 of a setting
    store = await _seeded()
    await sysconf.update_setting(
        sysconf.UpdateSettingCommand(
            tenant_id=TENANT_A,
            key="diagnostics.cache",
            value={"max_entries": 500, "ttl_seconds": 60},
            expected_version=1,
            actor_id=ADMIN_A,
        ),
        store=store,
        clock=_FixedClock(),
    )
    # // when a second writer replays expected_version=1
    # // then Conflict (no silent overwrite)
    with pytest.raises(Conflict):
        await sysconf.update_setting(
            sysconf.UpdateSettingCommand(
                tenant_id=TENANT_A,
                key="diagnostics.cache",
                value={"max_entries": 700, "ttl_seconds": 60},
                expected_version=1,
                actor_id=ADMIN_B,
            ),
            store=store,
            clock=_FixedClock(),
        )


async def test_update_setting__unknown_key__raises_validation_failed() -> None:
    """An undeclared setting key is rejected at validation time."""
    # // given seeded defaults
    store = await _seeded()
    # // when updating an unknown key // then ValidationFailed
    with pytest.raises(ValidationFailed):
        await sysconf.update_setting(
            sysconf.UpdateSettingCommand(
                tenant_id=TENANT_A,
                key="logging.nope",
                value={"level": "INFO", "sink": "none", "rotation": "daily"},
                expected_version=1,
                actor_id=ADMIN_A,
            ),
            store=store,
            clock=_FixedClock(),
        )


async def test_get_setting__unknown_key__raises_not_found() -> None:
    """Reading an undeclared key is 404 (never an empty default)."""
    # // given seeded defaults
    store = await _seeded()
    # // when reading an unknown key // then NotFound
    with pytest.raises(NotFound):
        await sysconf.get_setting(TENANT_A, "storage.tape", store=store)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("logging.cases", {"level": "VERBOSE", "sink": "none", "rotation": "daily"}),
        ("logging.cases", {"level": "INFO", "sink": "datadog", "rotation": "daily"}),
        ("logging.cases", {"level": "INFO", "sink": "none", "rotation": "minutely"}),
        ("diagnostics.cache", {"max_entries": 0, "ttl_seconds": 60}),
        ("diagnostics.cache", {"max_entries": 100, "ttl_seconds": 99_999}),
        (
            "diagnostics.services",
            {"outbox": "yes", "sla_scanner": True, "webhook_dispatcher": True},
        ),
        ("storage.volume", {"adapter": "tape-robot", "cache_mb": 256, "tier": "standard"}),
        ("storage.volume", {"adapter": "local-fs", "cache_mb": 0, "tier": "standard"}),
        ("storage.volume", {"adapter": "local-fs", "cache_mb": 256, "tier": "glacier"}),
        ("encryption.at_rest", {"enabled": True, "key_ref": ""}),
        ("encryption.at_rest", {"enabled": "yes", "key_ref": ""}),
    ],
)
async def test_update_setting__malformed_value__raises_validation_failed(
    key: str, value: dict[str, object]
) -> None:
    """Every malformed category value is rejected (no partial write)."""
    # // given seeded defaults
    store = await _seeded()
    # // when writing a malformed value // then ValidationFailed, version stays 1
    with pytest.raises(ValidationFailed):
        await sysconf.update_setting(
            sysconf.UpdateSettingCommand(
                tenant_id=TENANT_A,
                key=key,
                value=value,
                expected_version=1,
                actor_id=ADMIN_A,
            ),
            store=store,
            clock=_FixedClock(),
        )
    current = await sysconf.get_setting(TENANT_A, key, store=store)
    assert current.version == 1


async def test_update_setting__azure_blob_declared__accepted_as_config() -> None:
    """Declaring azure-blob is valid config data (enforcement is infra-only)."""
    # // given seeded defaults
    store = await _seeded()
    # // when declaring the Azure volume
    result = await sysconf.update_setting(
        sysconf.UpdateSettingCommand(
            tenant_id=TENANT_A,
            key="storage.volume",
            value={"adapter": "azure-blob", "cache_mb": 512, "tier": "standard"},
            expected_version=1,
            actor_id=ADMIN_A,
            change_summary="plan Azure move",
        ),
        store=store,
        clock=_FixedClock(),
    )
    # // then stored as declared config (the infra adapter fails closed at serve time)
    assert result.setting.value["adapter"] == "azure-blob"
    assert result.setting.version == 2


async def test_settings__secret_like_value__redacted_on_every_surface() -> None:
    """Secret-like keys are stored but never rendered (FM-011)."""
    # // given seeded defaults
    store = await _seeded()
    # // when a value embeds a secret-like key (forward-compat extra key)
    result = await sysconf.update_setting(
        sysconf.UpdateSettingCommand(
            tenant_id=TENANT_A,
            key="logging.audit",
            value={
                "level": "INFO",
                "sink": "splunk",
                "rotation": "daily",
                "api_key": "hunter2",
            },
            expected_version=1,
            actor_id=ADMIN_A,
        ),
        store=store,
        clock=_FixedClock(),
    )
    # // then list/get/audit surfaces all redact, store keeps verbatim
    assert result.audit.redacted_value["api_key"] == "[REDACTED]"
    listed = await sysconf.list_settings(TENANT_A, store=store)
    audit_row = next(s for s in listed if s.key == "logging.audit")
    assert audit_row.value["api_key"] == "[REDACTED]"
    single = await sysconf.get_setting(TENANT_A, "logging.audit", store=store)
    assert single.value["api_key"] == "[REDACTED]"
    raw = await store.latest(TENANT_A, "logging.audit")
    assert raw is not None
    assert raw.value["api_key"] == "hunter2"


async def test_settings__other_tenant__isolated_versions() -> None:
    """Tenant B sees defaults, never tenant A's versions (R10)."""
    # // given tenant A customized + tenant B seeded
    store = await _seeded()
    await sysconf.update_setting(
        sysconf.UpdateSettingCommand(
            tenant_id=TENANT_A,
            key="logging.cases",
            value={"level": "DEBUG", "sink": "none", "rotation": "daily"},
            expected_version=1,
            actor_id=ADMIN_A,
        ),
        store=store,
        clock=_FixedClock(),
    )
    await sysconf.ensure_defaults(TENANT_B, store=store, actor_id=ADMIN_B, clock=_FixedClock())
    # // when reading tenant B // then defaults only, history empty of A
    other = await sysconf.get_setting(TENANT_B, "logging.cases", store=store)
    assert other.value["level"] == "INFO"
    assert other.version == 1
    assert await store.history(TENANT_B, "logging.cases") != []
    own_history = await store.history(TENANT_A, "logging.cases")
    assert [s.version for s in own_history] == [1, 2]
    # // and an unseeded tenant lists nothing (no cross-tenant leak)
    assert await store.list_latest(UUID("33333333-3333-3333-3333-333333333333")) == []
