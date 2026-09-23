"""Tests for the connector lifecycle (P14-US5, US7 / FM-058).

Acceptance scenarios from ``specs/pillars/P14-integrations-migration/pillar.md``
US5, US7, N1, and the FM-058 acceptance narrative:

1. **Approve → enable → monitor → retire → drain**: a process
   administrator exercises the full lifecycle on a synthetic
   connector. Every transition appends one audit row and one outbox
   row.
2. **Safe-drain invariant**: a retire that reports ``in_flight > 0``
   leaves the connector in ``DRAINING`` (not ``RETIRED``); the
   operator re-issues once the count reaches zero.
3. **No duplicate connector without auth**: a named connector
   (any kind other than ``synthetic``) is deny-by-default. The
   ``CONNECTOR_NAMED_IMPLEMENT`` action is Gated (ADR-015) and no
   role grants it. The domain layer also refuses to construct a
   named row without ``implementation_authorized_by`` — the
   deterministic backstop of the application-layer policy.
4. **Authorization**: a non-admin actor (e.g. ``intake``) cannot
   approve, enable, monitor, or retire a connector.
5. **Synthetic connector only**: the first slice ships ``SYNTHETIC``
   only. The catalog reserves ``ONBASE`` / ``PANZURA`` for the Gated
   named-connector authorization.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. Tests are deterministic: fixed clock, fixed UUIDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.application.connectors import manage_connectors
from app.domain.connector import (
    Connector,
    ConnectorKind,
    ConnectorState,
)
from app.domain.errors import (
    Forbidden,
    InvariantViolation,
    NotFound,
    ValidationFailed,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_USER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
INTAKE_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

FIXED_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class InMemoryConnectorRepository:
    """In-memory ``ConnectorRepository``."""

    def __init__(self) -> None:
        self._rows: dict[UUID, Connector] = {}

    async def get(self, tenant_id: UUID, connector_id: UUID) -> Connector | None:
        row = self._rows.get(connector_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def get_by_key(self, tenant_id: UUID, key: str) -> Connector | None:
        for row in self._rows.values():
            if row.tenant_id == tenant_id and row.key == key:
                return row
        return None

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Connector]:
        out = [r for r in self._rows.values() if r.tenant_id == tenant_id]
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out[offset : offset + limit]

    async def add(self, connector: Connector) -> None:
        self._rows[connector.id] = connector

    async def update(self, connector: Connector) -> None:
        self._rows[connector.id] = connector


@dataclass
class StagedAudit:
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    subject_type: str
    subject_id: UUID
    payload: dict[str, Any]
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
                correlation_id=correlation_id,
            )
        )


@dataclass
class StagedOutbox:
    tenant_id: UUID
    event_type: str
    event_id: UUID
    payload: dict[str, Any]


class InMemoryOutbox:
    def __init__(self) -> None:
        self.events: list[StagedOutbox] = []

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        self.events.append(
            StagedOutbox(
                tenant_id=tenant_id,
                event_type=event_type,
                event_id=event_id,
                payload=payload,
            )
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def connector_repo() -> InMemoryConnectorRepository:
    return InMemoryConnectorRepository()


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def outbox() -> InMemoryOutbox:
    return InMemoryOutbox()


def _admin_draft_cmd(
    *,
    key: str = "synthetic-main",
    kind: ConnectorKind = ConnectorKind.SYNTHETIC,
    design_summary: str = "synthetic test connector",
    tenant_id: UUID = ACME_TENANT,
) -> manage_connectors.DraftConnectorCommand:
    return manage_connectors.DraftConnectorCommand(
        tenant_id=tenant_id,
        actor_id=ADMIN_USER,
        actor_roles=("process_admin",),
        key=key,
        kind=kind,
        design_summary=design_summary,
        authorization_rationale="",
        correlation_id="corr-conn-001",
    )


# ---------------------------------------------------------------------------
# 1. Full lifecycle: approve → enable → monitor → retire → drain
# ---------------------------------------------------------------------------


async def test_connector_lifecycle__approve_enable_monitor_retire_drain(
    clock: FixedClock,
    connector_repo: InMemoryConnectorRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    """A process administrator exercises the full lifecycle on a
    synthetic connector. Every transition appends one audit row and one
    outbox row. The safe-drain invariant is observed: ``in_flight=0``
    is the precondition for the terminal ``RETIRED`` transition.
    """
    # 1. Draft
    drafted = await manage_connectors.draft_connector(
        _admin_draft_cmd(),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert drafted.connector.state is ConnectorState.DRAFT
    assert drafted.connector.is_named() is False

    # 2. Approve
    approved = await manage_connectors.approve_connector(
        manage_connectors.ApproveConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
            rationale="approved by security admin for synthetic test",
            correlation_id="corr-conn-002",
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert approved.connector.state is ConnectorState.APPROVED
    assert approved.connector.approved_by == ADMIN_USER

    # 3. Enable
    enabled = await manage_connectors.enable_connector(
        manage_connectors.EnableConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
            correlation_id="corr-conn-003",
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert enabled.connector.state is ConnectorState.ENABLED
    assert enabled.connector.accepts_traffic() is True

    # 4. Monitor (no state change)
    monitored = await manage_connectors.monitor_connector(
        manage_connectors.MonitorConnectorQuery(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
        ),
        connector_repo=connector_repo,
    )
    assert monitored.connector.state is ConnectorState.ENABLED
    assert monitored.accepts_traffic is True
    assert monitored.is_terminal is False

    # 5. Retire with in_flight=0 → safe-drain then RETIRED in one call
    retired = await manage_connectors.retire_connector(
        manage_connectors.RetireConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
            drain_notes="drain completed cleanly",
            in_flight=0,
            completed=12,
            rejected=0,
            correlation_id="corr-conn-004",
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert retired.connector.state is ConnectorState.RETIRED
    assert retired.connector.is_terminal() is True
    assert retired.connector.accepts_traffic() is False
    assert retired.drained is True
    assert retired.connector.drain_confirmed is True
    assert retired.connector.in_flight_count == 0
    assert retired.connector.total_completed_count == 12

    # Audit chain captured every transition.
    event_types = [e.event_type for e in audit.events]
    assert event_types == [
        "ConnectorDrafted",
        "ConnectorApproved",
        "ConnectorEnabled",
        "ConnectorDrainStarted",
        "ConnectorDrainConfirmed",
        "ConnectorRetired",
    ]
    # Outbox chain captured every transition.
    outbox_types = [e.event_type for e in outbox.events]
    assert outbox_types == [
        "ConnectorDrafted",
        "ConnectorApproved",
        "ConnectorEnabled",
        "ConnectorDrainStarted",
        "ConnectorDrainConfirmed",
        "ConnectorRetired",
    ]


# ---------------------------------------------------------------------------
# 2. Safe-drain invariant: in_flight > 0 leaves the connector in DRAINING
# ---------------------------------------------------------------------------


async def test_retire_connector__in_flight_nonzero__leaves_in_draining(
    clock: FixedClock,
    connector_repo: InMemoryConnectorRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    """A retire that reports ``in_flight > 0`` leaves the connector in
    ``DRAINING`` (not ``RETIRED``). The operator re-issues the retire
    once the count reaches zero.

    This is the deterministic backstop of P14-US7: a connector that
    retires with in-flight work loses that work silently, which is a
    Constitution V violation.
    """
    drafted = await manage_connectors.draft_connector(
        _admin_draft_cmd(),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    await manage_connectors.approve_connector(
        manage_connectors.ApproveConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
            rationale="approved",
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    await manage_connectors.enable_connector(
        manage_connectors.EnableConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # First retire attempt: in_flight=3, the connector stays in DRAINING.
    first = await manage_connectors.retire_connector(
        manage_connectors.RetireConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
            drain_notes="draining in progress",
            in_flight=3,
            completed=5,
            rejected=0,
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert first.connector.state is ConnectorState.DRAINING
    assert first.drained is False
    assert first.connector.drain_confirmed is False
    assert first.connector.in_flight_count == 3

    # Monitor reflects the in-flight count.
    snapshot = await manage_connectors.monitor_connector(
        manage_connectors.MonitorConnectorQuery(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
        ),
        connector_repo=connector_repo,
    )
    assert snapshot.in_flight == 3
    assert snapshot.completed == 5
    assert snapshot.accepts_traffic is False  # DRAINING does not accept

    # Second retire attempt: in_flight=0, the connector transitions to RETIRED.
    second = await manage_connectors.retire_connector(
        manage_connectors.RetireConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
            drain_notes="drain complete",
            in_flight=0,
            completed=8,
            rejected=0,
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert second.connector.state is ConnectorState.RETIRED
    assert second.drained is True
    assert second.connector.in_flight_count == 0
    assert second.connector.total_completed_count == 8

    # Audit chain: drain started (once), drain confirmed (only the
    # safe-drain confirmation), retired.
    event_types = [e.event_type for e in audit.events]
    assert "ConnectorDrainStarted" in event_types
    assert event_types.count("ConnectorDrainConfirmed") == 1
    assert "ConnectorRetired" in event_types


# ---------------------------------------------------------------------------
# 3. Domain backstop: a named connector refuses to construct without
#    implementation_authorized_by (Constitution V.10, ADR-015).
# ---------------------------------------------------------------------------


def test_connector_aggregate__named_kind_without_authorization__raises() -> None:
    """The domain layer refuses to construct a named connector row
    without ``implementation_authorized_by``. This is the deterministic
    backstop of the application-layer policy: even if a future
    refactor forgets the ``authorize()`` call, the domain invariant
    holds.
    """
    with pytest.raises(InvariantViolation) as excinfo:
        Connector.draft(
            tenant_id=ACME_TENANT,
            key="onbase-prod",
            kind=ConnectorKind.ONBASE,
            design_summary="OnBase connector",
            created_by=ADMIN_USER,
            now=FIXED_NOW,
        )
    assert "implementation_authorized_by" in str(excinfo.value)
    assert "synthetic-only" in str(excinfo.value)


def test_connector_aggregate__named_kind_with_authorization__constructs() -> None:
    """A named connector with the explicit authorization record
    constructs successfully. The domain check is the deterministic
    backstop; the application-layer policy is the policy half.
    """
    connector = Connector.draft(
        tenant_id=ACME_TENANT,
        key="onbase-prod",
        kind=ConnectorKind.ONBASE,
        design_summary="OnBase connector (authorized)",
        created_by=ADMIN_USER,
        now=FIXED_NOW,
        implementation_authorized_by=ADMIN_USER,
        implementation_authorized_at=FIXED_NOW,
        authorization_rationale="explicit per ADR-015",
    )
    assert connector.kind is ConnectorKind.ONBASE
    assert connector.implementation_authorized_by == ADMIN_USER
    assert connector.is_named() is True


def test_connector_aggregate__named_kind_with_empty_rationale__raises() -> None:
    """A named connector with a blank rationale is refused. The
    domain requires a non-empty rationale so the audit chain has
    a real artifact to surface.
    """
    with pytest.raises(InvariantViolation):
        Connector.draft(
            tenant_id=ACME_TENANT,
            key="onbase-prod",
            kind=ConnectorKind.ONBASE,
            design_summary="OnBase connector",
            created_by=ADMIN_USER,
            now=FIXED_NOW,
            implementation_authorized_by=ADMIN_USER,
            implementation_authorized_at=FIXED_NOW,
            authorization_rationale="   ",
        )


# ---------------------------------------------------------------------------
# 4. Application-layer Gated gate: drafting a named connector is
#    deny-by-default. No role grants CONNECTOR_NAMED_IMPLEMENT today.
# ---------------------------------------------------------------------------


async def test_draft_named_connector__process_admin__raises_forbidden(
    clock: FixedClock,
    connector_repo: InMemoryConnectorRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    """A process administrator (the highest non-system role in the
    first slice) is denied when drafting a named connector. The
    ``CONNECTOR_NAMED_IMPLEMENT`` action is Gated per ADR-015; no
    role grants it today.

    The denial surfaces BEFORE the domain check: the policy layer
    refuses the action so the audit + outbox + repository are
    untouched.
    """
    with pytest.raises(Forbidden):
        await manage_connectors.draft_connector(
            _admin_draft_cmd(kind=ConnectorKind.ONBASE),
            connector_repo=connector_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    # Nothing was written.
    assert connector_repo._rows == {}
    assert audit.events == []
    assert outbox.events == []


async def test_draft_synthetic_connector__process_admin__succeeds(
    clock: FixedClock,
    connector_repo: InMemoryConnectorRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    """A process administrator can draft a **synthetic** connector
    (the only kind the first slice ships with). The named-implement
    gate is bypassed because ``kind=SYNTHETIC`` is on the
    unrestricted list.
    """
    drafted = await manage_connectors.draft_connector(
        _admin_draft_cmd(),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert drafted.connector.kind is ConnectorKind.SYNTHETIC
    assert drafted.connector.is_named() is False
    assert drafted.connector.state is ConnectorState.DRAFT
    assert len(audit.events) == 1
    assert audit.events[0].event_type == "ConnectorDrafted"


# ---------------------------------------------------------------------------
# 5. Authorization: a non-admin actor cannot approve / enable / monitor /
#    retire. The check is the application-layer ``authorize()`` call;
#    the domain layer is intentionally not consulted.
# ---------------------------------------------------------------------------


async def test_approve_connector__intake_role__raises_forbidden(
    clock: FixedClock,
    connector_repo: InMemoryConnectorRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    """An ``intake`` user (no ``ADMIN_TENANT_CONFIG``) cannot approve
    a connector. The use case raises ``Forbidden`` before touching
    the domain.
    """
    drafted = await manage_connectors.draft_connector(
        _admin_draft_cmd(),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    with pytest.raises(Forbidden):
        await manage_connectors.approve_connector(
            manage_connectors.ApproveConnectorCommand(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
                connector_id=drafted.connector.id,
                rationale="r",
            ),
            connector_repo=connector_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


async def test_enable_connector__reviewer_role__raises_forbidden(
    clock: FixedClock,
    connector_repo: InMemoryConnectorRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    """A ``reviewer`` user (no ``ADMIN_TENANT_CONFIG``) cannot enable
    a connector even after it is approved.
    """
    drafted = await manage_connectors.draft_connector(
        _admin_draft_cmd(),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    await manage_connectors.approve_connector(
        manage_connectors.ApproveConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
            rationale="r",
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    with pytest.raises(Forbidden):
        await manage_connectors.enable_connector(
            manage_connectors.EnableConnectorCommand(
                tenant_id=ACME_TENANT,
                actor_id=REVIEWER_USER,
                actor_roles=("reviewer",),
                connector_id=drafted.connector.id,
            ),
            connector_repo=connector_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 6. Cross-tenant: a process administrator in tenant A cannot approve
#    a connector belonging to tenant B. The application layer never
#    reveals the existence of the foreign row (Constitution V).
# ---------------------------------------------------------------------------


async def test_approve_connector__cross_tenant__raises_not_found(
    clock: FixedClock,
    connector_repo: InMemoryConnectorRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    """Cross-tenant approve is surfaced as ``NotFound`` — the API
    never confirms the existence of a foreign tenant's row.
    """
    drafted = await manage_connectors.draft_connector(
        _admin_draft_cmd(),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    with pytest.raises(NotFound):
        await manage_connectors.approve_connector(
            manage_connectors.ApproveConnectorCommand(
                tenant_id=OTHER_TENANT,
                actor_id=ADMIN_USER,
                actor_roles=("process_admin",),
                connector_id=drafted.connector.id,
                rationale="r",
            ),
            connector_repo=connector_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 7. Idempotency: a re-approval of an already-approved connector
#    returns the same row without appending a duplicate audit event.
# ---------------------------------------------------------------------------


async def test_approve_connector__already_approved__is_idempotent(
    clock: FixedClock,
    connector_repo: InMemoryConnectorRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    """A re-approval is idempotent. The operator UI may retry on a
    flaky connection; the audit chain records the approval exactly
    once.
    """
    drafted = await manage_connectors.draft_connector(
        _admin_draft_cmd(),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    first = await manage_connectors.approve_connector(
        manage_connectors.ApproveConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
            rationale="first",
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    second = await manage_connectors.approve_connector(
        manage_connectors.ApproveConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
            rationale="retry",
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert first.connector is second.connector
    assert sum(1 for e in audit.events if e.event_type == "ConnectorApproved") == 1


# ---------------------------------------------------------------------------
# 8. State machine: invalid transitions are refused.
# ---------------------------------------------------------------------------


async def test_enable_connector__before_approve__raises_invariant(
    clock: FixedClock,
    connector_repo: InMemoryConnectorRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    """Enabling a DRAFT connector (skipping APPROVED) is refused.
    The state machine is one-way; skipping a state is not allowed.
    """
    drafted = await manage_connectors.draft_connector(
        _admin_draft_cmd(),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    with pytest.raises(InvariantViolation):
        await manage_connectors.enable_connector(
            manage_connectors.EnableConnectorCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                actor_roles=("process_admin",),
                connector_id=drafted.connector.id,
            ),
            connector_repo=connector_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


async def test_approve_connector__empty_rationale__raises_validation(
    clock: FixedClock,
    connector_repo: InMemoryConnectorRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    """An approval with an empty rationale is refused. The audit
    chain requires a real artifact to surface. The domain layer
    raises :class:`InvariantViolation` for an empty rationale
    (mirrors the channel's policy-validation pattern).
    """
    drafted = await manage_connectors.draft_connector(
        _admin_draft_cmd(),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    with pytest.raises(InvariantViolation):
        await manage_connectors.approve_connector(
            manage_connectors.ApproveConnectorCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                actor_roles=("process_admin",),
                connector_id=drafted.connector.id,
                rationale="   ",
            ),
            connector_repo=connector_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


async def test_retire_connector__negative_in_flight__raises_validation(
    clock: FixedClock,
    connector_repo: InMemoryConnectorRepository,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutbox,
) -> None:
    """A negative ``in_flight`` count is refused. The operator's
    snapshot must be non-negative; the use case translates the
    refusal to a structured ``ValidationFailed``.
    """
    drafted = await manage_connectors.draft_connector(
        _admin_draft_cmd(),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    await manage_connectors.approve_connector(
        manage_connectors.ApproveConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
            rationale="r",
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    await manage_connectors.enable_connector(
        manage_connectors.EnableConnectorCommand(
            tenant_id=ACME_TENANT,
            actor_id=ADMIN_USER,
            actor_roles=("process_admin",),
            connector_id=drafted.connector.id,
        ),
        connector_repo=connector_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    with pytest.raises(ValidationFailed):
        await manage_connectors.retire_connector(
            manage_connectors.RetireConnectorCommand(
                tenant_id=ACME_TENANT,
                actor_id=ADMIN_USER,
                actor_roles=("process_admin",),
                connector_id=drafted.connector.id,
                drain_notes="bad",
                in_flight=-1,
                completed=0,
                rejected=0,
            ),
            connector_repo=connector_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
