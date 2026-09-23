"""Tests for the domain event envelope (P13 / FM-055, foundation).

These tests pin the public API of ``app.domain.events.DomainEvent``
so future event types (CaseCreated, TaskCompleted, etc.) cannot
silently drift from the documented contract.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest

from app.domain.events import DomainEvent


def test_domain_event__minimal_construction__uses_defaults() -> None:
    """A bare ``DomainEvent`` carries the event_type, tenant_id, an
    auto-generated event_id, an empty payload, and no correlation
    ids. Defaults are stable so subscribers can rely on them.
    """
    tenant_id = uuid4()
    evt = DomainEvent(event_type="CaseCreated", tenant_id=tenant_id)

    assert evt.event_type == "CaseCreated"
    assert evt.tenant_id == tenant_id
    assert isinstance(evt.event_id, UUID)
    assert evt.payload == {}
    assert evt.correlation_id is None
    assert evt.causation_id is None


def test_domain_event__frozen__does_not_allow_mutation() -> None:
    """A frozen dataclass refuses attribute assignment; tests assert
    the invariant so accidental ``@dataclass`` decorator drift is
    caught at test time.
    """
    evt = DomainEvent(event_type="CaseCreated", tenant_id=uuid4())
    with pytest.raises(FrozenInstanceError):
        evt.event_type = "CaseUpdated"  # type: ignore[misc]


def test_domain_event__with_payload__merges_into_existing_payload() -> None:
    """``with_payload`` returns a new instance with merged payload
    keys. The original instance is untouched (frozen dataclass).
    """
    tenant_id = uuid4()
    evt = DomainEvent(
        event_type="CaseCreated",
        tenant_id=tenant_id,
        payload={"actor_id": "u-1"},
    )
    augmented = evt.with_payload(case_id="c-1", reference="C-ACME-2026-000001")

    assert evt.payload == {"actor_id": "u-1"}  # unchanged
    assert augmented.payload == {
        "actor_id": "u-1",
        "case_id": "c-1",
        "reference": "C-ACME-2026-000001",
    }
    # Identity is preserved across with_payload.
    assert augmented.event_id == evt.event_id
    assert augmented.event_type == evt.event_type
    assert augmented.tenant_id == evt.tenant_id


def test_domain_event__with_payload__overrides_existing_key() -> None:
    """A duplicate key in ``with_payload`` overrides the existing
    value. The semantics are merge-with-override (dict.update), not
    error-on-duplicate.
    """
    evt = DomainEvent(
        event_type="CaseCreated",
        tenant_id=uuid4(),
        payload={
            "case_id": str(uuid4()),
            "reference": "C-ACME-2026-000001",
            "case_type_key": "ServiceRequest",
            "tenant_id": str(uuid4()),
            "actor_id": str(uuid4()),
            "occurred_at": "2026-09-02T00:00:00Z",
        },
    )
    augmented = evt.with_payload(
        case_id="c-overridden",
        reference="C-ACME-2026-000002",
    )
    assert augmented.payload["case_id"] == "c-overridden"
    assert augmented.payload["reference"] == "C-ACME-2026-000002"
    # The original payload keys survive the merge.
    assert augmented.payload["case_type_key"] == "ServiceRequest"


def test_domain_event__correlation_ids__propagate_to_with_payload() -> None:
    """Correlation and causation ids are preserved across
    ``with_payload``. Subscribers that build extended events from a
    base can rely on the id chain.
    """
    evt = DomainEvent(
        event_type="CaseCreated",
        tenant_id=uuid4(),
        correlation_id="corr-001",
        causation_id="cause-001",
        payload={
            "case_id": str(uuid4()),
            "reference": "C-ACME-2026-000001",
            "case_type_key": "ServiceRequest",
            "tenant_id": str(uuid4()),
            "actor_id": str(uuid4()),
            "occurred_at": "2026-09-02T00:00:00Z",
        },
    )
    extended = evt.with_payload(
        case_id="c-new",
        reference="C-ACME-2026-000002",
    )
    assert extended.correlation_id == "corr-001"
    assert extended.causation_id == "cause-001"
