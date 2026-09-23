"""Tests for the extension registry use case (P13-US12 / FM-047).

Covers the acceptance criteria from the dispatch:

- register succeeds: a valid ``ExtensionSpec`` lands in the registry
  and ``installed=True`` is returned;
- invalid rejected: shape / invariant failures raise
  ``ExtensionValidationError`` (mapped to ``422`` at the HTTP boundary);
- audited: every successful install appends an ``ExtensionRegistered``
  row carrying the full spec so a future replay CLI can reconstruct it;
- duplicate version rejected: a second registration at the same
  ``(module_id, version)`` with a different shape raises ``Conflict``;
  an identical shape is a no-op idempotent retry.

Tests use in-memory fakes for the audit store, the clock, and the
registry so they run against the sqlite profile without Docker
(``AGENTS.md §4.R1``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.extensions import register_extension
from app.deps import UserContext
from app.domain.errors import Conflict, Forbidden
from app.domain.extension import (
    EventTypeSpec,
    ExtensionSpec,
    ExtensionValidationError,
    PolicyActionSpec,
    build_registry,
    validate_extension,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FixedClock:
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or NOW

    def now(self) -> datetime:
        return self._now


@dataclass
class StagedAudit:
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    subject_type: str
    subject_id: UUID
    payload: dict[str, Any]
    occurred_at: datetime
    correlation_id: str


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.events: list[StagedAudit] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        self.events.append(
            StagedAudit(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                subject_type=subject_type,
                subject_id=subject_id,
                payload=payload,
                occurred_at=occurred_at,
                correlation_id=correlation_id,
            )
        )


def _admin_user(tenant_id: UUID = TENANT_A) -> UserContext:
    """Build a tenant-admin ``UserContext``.

    ``app/application/authorization.py`` consults the role set on the
    user; passing ``process_admin`` here is enough to grant
    ``ADMIN_TENANT_CONFIG`` for the registration path (see the role
    catalog in ``app/application/authorization.py``).
    """
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant_id,
        display_name="Test Admin",
        roles=("process_admin",),
        is_system=False,
    )


def _non_admin_user(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant_id,
        display_name="Test Intake",
        roles=("intake",),
        is_system=False,
    )


# ---------------------------------------------------------------------------
# Builders for valid spec input
# ---------------------------------------------------------------------------


def _action(action: str = "ext.demo.create") -> PolicyActionSpec:
    return PolicyActionSpec(
        action=action,
        description="Create a demo record",
        is_sensitive=False,
    )


def _event(
    event_type: str = "TenantExt.DemoCreated",
    version: int = 1,
) -> EventTypeSpec:
    return EventTypeSpec(
        event_type=event_type,
        version=version,
        description="A demo event type",
        payload_keys=("tenant_id", "actor_id", "occurred_at"),
    )


def _command(
    user: UserContext | None = None,
    *,
    module_id: str = "ext.demo",
    version: int = 1,
    description: str = "Demo extension module",
    actions: tuple[PolicyActionSpec, ...] = (_action(),),
    events: tuple[EventTypeSpec, ...] = (_event(),),
    author: str = "platform@example.test",
    metadata: dict[str, Any] | None = None,
) -> register_extension.RegisterExtensionCommand:
    return register_extension.RegisterExtensionCommand(
        tenant_id=(user or _admin_user()).tenant_id,
        actor=user or _admin_user(),
        correlation_id="corr-test",
        module_id=module_id,
        version=version,
        description=description,
        policy_actions=actions,
        event_types=events,
        author=author,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Domain validation (pure-function tests)
# ---------------------------------------------------------------------------


def test_validate_extension__accepts_a_well_formed_spec() -> None:
    """Happy path: ``validate_extension`` does not raise on a spec that
    meets every invariant (namespaced action, PascalCase.dotted event,
    payload keys free of PHI needles, etc.)."""
    spec = ExtensionSpec(
        module_id="ext.demo",
        version=1,
        description="Demo",
        policy_actions=(_action(),),
        event_types=(_event(),),
        author="platform@example.test",
    )
    validate_extension(spec)  # does not raise


def test_validate_extension__rejects_action_missing_module_prefix() -> None:
    """An action that does not begin with the module id is rejected so
    extensions cannot squat on a sibling module's namespace."""
    spec = ExtensionSpec(
        module_id="ext.demo",
        version=1,
        description="Demo",
        policy_actions=(
            PolicyActionSpec(
                action="ext.other.create",
                description="Squatter",
                is_sensitive=False,
            ),
        ),
        event_types=(),
    )
    with pytest.raises(ExtensionValidationError):
        validate_extension(spec)


def test_validate_extension__rejects_phi_named_payload_key() -> None:
    """A payload key whose name looks like PHI is rejected at
    registration time (not at publish time) so a careless module never
    reaches a subscriber."""
    spec = ExtensionSpec(
        module_id="ext.demo",
        version=1,
        description="Demo",
        policy_actions=(),
        event_types=(
            EventTypeSpec(
                event_type="TenantExt.PIILeak",
                version=1,
                description="oops",
                payload_keys=("tenant_id", "patient_email"),
            ),
        ),
    )
    with pytest.raises(ExtensionValidationError):
        validate_extension(spec)


def test_validate_extension__rejects_event_type_without_namespace() -> None:
    """``CaseCreated`` style (no namespace) is rejected so every
    extension-declared type is namespaced and deprecation can target it
    precisely."""
    spec = ExtensionSpec(
        module_id="ext.demo",
        version=1,
        description="Demo",
        policy_actions=(),
        event_types=(
            EventTypeSpec(
                event_type="BareEventType",
                version=1,
                description="missing namespace",
                payload_keys=(),
            ),
        ),
    )
    with pytest.raises(ExtensionValidationError):
        validate_extension(spec)


# ---------------------------------------------------------------------------
# Use case: success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_extension__valid_spec__installs_and_audits() -> None:
    """A valid spec lands in the registry, the audit store records one
    ``ExtensionRegistered`` row carrying the full payload, and the
    returned ``RegisterResult.installed`` is True on first install."""
    audit = InMemoryAuditStore()
    registry = build_registry()

    result = await register_extension.register_extension(
        _command(),
        registry=registry,
        audit=audit,
        clock=FixedClock(),
    )

    # First install succeeds and reports installed=True.
    assert result.installed is True
    assert result.module.spec.module_id == "ext.demo"
    assert result.module.spec.version == 1

    # Registry exposes the module on both reads.
    assert len(registry.list_modules()) == 1
    assert registry.has_action("ext.demo.create") is True
    assert registry.find_event_spec("TenantExt.DemoCreated") == _event()

    # Audit row carries the full spec for replay.
    assert len(audit.events) == 1
    evt = audit.events[0]
    assert evt.event_type == register_extension.EXTENSION_REGISTERED
    assert evt.tenant_id == TENANT_A
    assert evt.subject_type == register_extension.SUBJECT_TYPE_EXTENSION
    assert evt.payload["module_id"] == "ext.demo"
    assert evt.payload["version"] == 1
    assert evt.payload["policy_actions"] == [
        {
            "action": "ext.demo.create",
            "description": "Create a demo record",
            "is_sensitive": False,
        }
    ]
    assert evt.payload["event_types"] == [
        {
            "event_type": "TenantExt.DemoCreated",
            "version": 1,
            "description": "A demo event type",
            "payload_keys": ["tenant_id", "actor_id", "occurred_at"],
        }
    ]
    assert evt.correlation_id == "corr-test"


# ---------------------------------------------------------------------------
# Use case: invalid input rejected (422 path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_extension__invalid_action__rejected_no_audit() -> None:
    """A bad action name fails the domain validator; the audit store
    stays empty because the spec never validates."""
    audit = InMemoryAuditStore()
    registry = build_registry()

    with pytest.raises(ExtensionValidationError):
        await register_extension.register_extension(
            _command(
                actions=(
                    PolicyActionSpec(
                        action="case.create",  # not namespaced, would shadow built-in
                        description="bad",
                    ),
                ),
            ),
            registry=registry,
            audit=audit,
            clock=FixedClock(),
        )

    assert audit.events == []
    assert registry.list_modules() == ()


# ---------------------------------------------------------------------------
# Use case: duplicate version rejected (409 path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_extension__duplicate_version_same_shape__idempotent_noop() -> None:
    """An identical re-registration of ``(module_id, version)`` is a
    silent no-op (idempotent retry): ``installed=False``, no second
    audit row, registry unchanged."""
    registry = build_registry()
    audit = InMemoryAuditStore()

    first = await register_extension.register_extension(
        _command(),
        registry=registry,
        audit=audit,
        clock=FixedClock(),
    )
    second = await register_extension.register_extension(
        _command(),
        registry=registry,
        audit=audit,
        clock=FixedClock(),
    )

    assert first.installed is True
    assert second.installed is False
    assert second.module.installed_at == first.module.installed_at
    assert len(audit.events) == 1
    assert len(registry.list_modules()) == 1


@pytest.mark.asyncio
async def test_register_extension__duplicate_version_different_shape__conflict() -> None:
    """A second registration at the same ``(module_id, version)`` with a
    different shape raises ``Conflict`` (the use case maps
    ``ExtensionConflictError`` to ``Conflict``); the audit store does
    NOT receive a second row, and the original install is preserved.
    """
    registry = build_registry()
    audit = InMemoryAuditStore()

    await register_extension.register_extension(
        _command(),
        registry=registry,
        audit=audit,
        clock=FixedClock(),
    )

    with pytest.raises(Conflict):
        await register_extension.register_extension(
            _command(description="A different description for the same key"),
            registry=registry,
            audit=audit,
            clock=FixedClock(),
        )

    # Only the first install audited.
    assert len(audit.events) == 1
    # Registry still has the original description.
    [module] = registry.list_modules()
    assert module.spec.description == "Demo extension module"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_extension__non_admin__forbidden() -> None:
    """A non-admin caller is rejected by the authorization policy
    before any state changes; the registry stays empty and the audit
    store receives no row."""
    audit = InMemoryAuditStore()
    registry = build_registry()

    with pytest.raises(Forbidden):
        await register_extension.register_extension(
            _command(user=_non_admin_user()),
            registry=registry,
            audit=audit,
            clock=FixedClock(),
        )

    assert audit.events == []
    assert registry.list_modules() == ()


# ---------------------------------------------------------------------------
# Tenant-defined audit type appears in catalog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_extension__tenant_event_type__appears_in_event_catalog() -> None:
    """The acceptance scenario: a tenant-defined event type is registered
    and shows up in the registry's event catalog so the existing
    ``iter_event_specs`` helper can union it with the built-in catalog."""
    registry = build_registry()
    audit = InMemoryAuditStore()

    await register_extension.register_extension(
        _command(
            events=(
                EventTypeSpec(
                    event_type="TenantExt.Proof",
                    version=1,
                    description="Tenant audit proof",
                    payload_keys=("tenant_id", "occurred_at"),
                ),
            ),
        ),
        registry=registry,
        audit=audit,
        clock=FixedClock(),
    )

    catalog = registry.list_event_specs()
    assert any(e.event_type == "TenantExt.Proof" and e.version == 1 for e in catalog)
    assert registry.find_event_spec("TenantExt.Proof").version == 1
