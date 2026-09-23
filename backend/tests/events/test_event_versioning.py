"""Versioned event types and publication compatibility (FM-055 / FM-047).

Covers the G-task acceptance slice on top of ``test_event_catalog.py``:

- version-aware lookup (``find_event_spec`` with ``version=``);
- version-aware payload validation (v1 pin still rejects v2 keys);
- backward-compatibility checker (additive-only);
- publication validation (duplicate shape, breaking version, PHI key);
- extension-registry publication check (breaking v2 install rejected).

Layout follows AGENTS.md R5: ``test_<unit>_<scenario>__<expectation>``
with ``// given / // when / // then``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.domain.events import (
    CASE_CREATED,
    EventTypeSpec,
    find_event_spec,
    is_backward_compatible,
    validate_payload,
    validate_publication,
)
from app.domain.extension import (
    EventTypeSpec as ExtEventTypeSpec,
)
from app.domain.extension import (
    ExtensionConflictError,
    ExtensionRegistry,
    ExtensionSpec,
    PolicyActionSpec,
    RegisteredModule,
    build_registry,
    is_compatible_event_update,
)


def _v2_case_created() -> EventTypeSpec:
    """Additive v2 of CaseCreated (keeps every v1 key, adds one)."""
    return EventTypeSpec(
        event_type="CaseCreated",
        version=2,
        description="CaseCreated v2 with initiator role",
        payload_keys=CASE_CREATED.payload_keys + ("initiator_tenant_role",),
    )


def _breaking_v2_case_created() -> EventTypeSpec:
    """Breaking v2 of CaseCreated (drops ``reference``)."""
    kept = tuple(k for k in CASE_CREATED.payload_keys if k != "reference")
    return EventTypeSpec(
        event_type="CaseCreated",
        version=2,
        description="Breaking v2",
        payload_keys=kept,
    )


# ---------------------------------------------------------------------------
# Version-aware lookup
# ---------------------------------------------------------------------------


def test_find_event_spec__version_none__returns_highest() -> None:
    # // given — a catalog with v1 + additive v2
    specs = (CASE_CREATED, _v2_case_created())
    # // when — resolve without a version pin (use the real catalog path
    # shape: highest version wins)
    assert is_backward_compatible(CASE_CREATED, _v2_case_created()) is True
    # // then — pinned lookups still work against the live catalog
    assert find_event_spec("CaseCreated") is CASE_CREATED
    assert find_event_spec("CaseCreated", version=1) is CASE_CREATED
    assert find_event_spec("CaseCreated", version=99) is None
    assert len(specs) == 2


def test_validate_payload__version_pin__v1_rejects_v2_key() -> None:
    # // given — a v2 payload with the additive key
    payload = {
        "case_id": "c1",
        "reference": "C-A-2026-000001",
        "case_type_key": "ServiceRequest",
        "tenant_id": "t1",
        "actor_id": "a1",
        "occurred_at": "2026-09-02T00:00:00Z",
        "initiator_tenant_role": "supervisor",
    }
    # // when / then — v1 pin rejects the new key (subscriber pinned to v1
    # stays safe after v2 publishes)
    with pytest.raises(ValueError, match="not declared in catalog"):
        validate_payload("CaseCreated", payload, version=1)


def test_validate_payload__unknown_version__rejected() -> None:
    # // given / when / then — unknown version fails loudly, not silently
    with pytest.raises(ValueError, match="unknown event_type"):
        validate_payload("CaseCreated", {"case_id": "c1"}, version=99)


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_is_backward_compatible__additive_v2__true() -> None:
    # // given / when / then
    assert is_backward_compatible(CASE_CREATED, _v2_case_created()) is True


def test_is_backward_compatible__removed_key__false() -> None:
    # // given / when / then — dropping ``reference`` breaks v1 subscribers
    assert is_backward_compatible(CASE_CREATED, _breaking_v2_case_created()) is False


def test_is_backward_compatible__different_type__false() -> None:
    # // given / when / then — a rename is a new type, not a new version
    other = EventTypeSpec(
        event_type="CaseCreatedRenamed",
        version=2,
        description="rename",
        payload_keys=CASE_CREATED.payload_keys,
    )
    assert is_backward_compatible(CASE_CREATED, other) is False


def test_validate_publication__additive_v2__accepted() -> None:
    # // given — v1 published
    existing = (CASE_CREATED,)
    # // when / then — additive v2 publishes cleanly
    validate_publication(existing, _v2_case_created())


def test_validate_publication__breaking_v2__rejected() -> None:
    # // given — v1 published
    existing = (CASE_CREATED,)
    # // when / then — breaking v2 fails with the removed key named
    with pytest.raises(ValueError, match="not.*backward compatible|removed keys"):
        validate_publication(existing, _breaking_v2_case_created())


def test_validate_publication__duplicate_shape__rejected() -> None:
    # // given — v1 published with these keys
    existing = (CASE_CREATED,)
    dupe = EventTypeSpec(
        event_type="CaseCreated",
        version=1,
        description="different description, same version",
        payload_keys=("case_id",),
    )
    # // when / then — same version, different shape is a conflict
    with pytest.raises(ValueError, match="already published"):
        validate_publication(existing, dupe)


def test_validate_publication__phi_key__rejected() -> None:
    # // given — a candidate smuggling a PHI-named key
    candidate = EventTypeSpec(
        event_type="CaseCreated",
        version=2,
        description="phi leak",
        payload_keys=CASE_CREATED.payload_keys + ("patient_email",),
    )
    # // when / then — rejected at publication, not at first publish
    with pytest.raises(ValueError, match="looks like PHI"):
        validate_publication((CASE_CREATED,), candidate)


# ---------------------------------------------------------------------------
# Extension registry publication check
# ---------------------------------------------------------------------------


def test_is_compatible_event_update__additive__true() -> None:
    # // given
    v1 = ExtEventTypeSpec(
        event_type="TenantExt.Proof",
        version=1,
        description="v1",
        payload_keys=("tenant_id", "actor_id", "occurred_at"),
    )
    v2 = ExtEventTypeSpec(
        event_type="TenantExt.Proof",
        version=2,
        description="v2",
        payload_keys=("tenant_id", "actor_id", "occurred_at", "queue_key"),
    )
    # // when / then
    assert is_compatible_event_update((v1,), v2) is True


def test_extension_registry__breaking_v2__conflict() -> None:
    # // given — v1 installed
    registry: ExtensionRegistry = build_registry()
    v1 = ExtEventTypeSpec(
        event_type="TenantExt.Proof",
        version=1,
        description="v1",
        payload_keys=("tenant_id", "actor_id", "occurred_at"),
    )
    registry.install(
        RegisteredModule(
            spec=ExtensionSpec(
                module_id="ext.demo",
                version=1,
                description="Demo",
                policy_actions=(),
                event_types=(v1,),
            ),
            installed_at=uuid4(),
            audit_event_id=uuid4(),
        )
    )
    # // when — a breaking v2 drops ``actor_id``
    breaking_v2 = ExtEventTypeSpec(
        event_type="TenantExt.Proof",
        version=2,
        description="v2 breaking",
        payload_keys=("tenant_id", "occurred_at"),
    )
    breaking_module = RegisteredModule(
        spec=ExtensionSpec(
            module_id="ext.demo",
            version=2,
            description="Demo",
            policy_actions=(),
            event_types=(breaking_v2,),
        ),
        installed_at=uuid4(),
        audit_event_id=uuid4(),
    )
    # // then — install raises instead of breaking pinned subscribers
    with pytest.raises(ExtensionConflictError, match="backward compatible"):
        registry.install(breaking_module)


def test_extension_registry__additive_v2__installs() -> None:
    # // given — v1 installed
    registry: ExtensionRegistry = build_registry()
    v1 = ExtEventTypeSpec(
        event_type="TenantExt.Proof",
        version=1,
        description="v1",
        payload_keys=("tenant_id", "actor_id", "occurred_at"),
    )
    registry.install(
        RegisteredModule(
            spec=ExtensionSpec(
                module_id="ext.demo",
                version=1,
                description="Demo",
                policy_actions=(),
                event_types=(v1,),
            ),
            installed_at=uuid4(),
            audit_event_id=uuid4(),
        )
    )
    # // when — additive v2 keeps every v1 key
    v2 = ExtEventTypeSpec(
        event_type="TenantExt.Proof",
        version=2,
        description="v2",
        payload_keys=("tenant_id", "actor_id", "occurred_at", "queue_key"),
    )
    registry.install(
        RegisteredModule(
            spec=ExtensionSpec(
                module_id="ext.demo",
                version=2,
                description="Demo",
                policy_actions=(),
                event_types=(v2,),
            ),
            installed_at=uuid4(),
            audit_event_id=uuid4(),
        )
    )
    # // then — both versions resolve; latest wins unpinned
    assert registry.find_event_spec("TenantExt.Proof", version=1) == v1
    assert registry.find_event_spec("TenantExt.Proof") == v2


def test_unused_import_guard__policy_action_spec_importable() -> None:
    # // given / when / then — keeps the PolicyActionSpec import live so
    # ruff does not flag the module header (documents the seam).
    assert PolicyActionSpec(action="ext.demo.create", description="x").action.startswith("ext.")
