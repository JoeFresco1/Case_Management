"""Connector lifecycle application use cases (P14-US5, US7 / FM-058).

These operations coordinate authorization, the connector repository, the
audit chain, and the outbox. Domain state transitions remain in
``app.domain.connector``; all persistence writes are tenant-scoped and
share the request transaction.

# INTEGRATION_SEAM: connector adapters

The synthetic adapter is local-only. Production connector calls stay
behind the infrastructure seam in ``app.infrastructure.connectors``.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.connector import (
    LIFECYCLE_APPROVED,
    LIFECYCLE_DRAFT,
    LIFECYCLE_DRAINING,
    LIFECYCLE_ENABLED,
    Connector,
    ConnectorKind,
    is_terminal_state,
)
from app.domain.errors import ValidationFailed as DomainValidationFailed

from .commands import (
    ApproveConnectorCommand,
    ApproveConnectorResult,
    DraftConnectorCommand,
    DraftConnectorResult,
    EnableConnectorCommand,
    EnableConnectorResult,
    MonitorConnectorQuery,
    MonitorConnectorResult,
    RetireConnectorCommand,
    RetireConnectorResult,
)
from .constants import (
    DEFAULT_SYNTHETIC_ADAPTER_REF,
    EVENT_CONNECTOR_APPROVED,
    EVENT_CONNECTOR_DRAFTED,
    EVENT_CONNECTOR_DRAIN_CONFIRMED,
    EVENT_CONNECTOR_DRAIN_STARTED,
    EVENT_CONNECTOR_ENABLED,
    EVENT_CONNECTOR_RETIRED,
)
from .helpers import _emit_audit, _load, _user_context


async def draft_connector(
    cmd: DraftConnectorCommand,
    *,
    connector_repo: Any,
    audit: Any,
    outbox: Any,
    clock: Any,
) -> DraftConnectorResult:
    """Draft a fresh connector in ``DRAFT`` state.

    The named-connector authorization gate: a named kind
    (``ONBASE``/``PANZURA``/...) requires BOTH
    ``ADMIN_TENANT_CONFIG`` AND ``CONNECTOR_NAMED_IMPLEMENT``. The
    synthetic kind only requires ``ADMIN_TENANT_CONFIG``. The domain
    layer independently refuses to construct a named connector
    without ``implementation_authorized_by`` (Constitution V.10,
    ADR-015); the application layer's check is the policy half, the
    domain's check is the deterministic backstop.
    """
    # 1. Admin action — every connector operation is a process
    #    administrator operation.
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="connector"),
    )

    # 2. Named-connector gate. Deny-by-default: no role grants
    #    ``CONNECTOR_NAMED_IMPLEMENT`` today (ADR-015), so a named
    #    connector is always Forbidden until the product decision
    #    is made.
    if cmd.kind is not ConnectorKind.SYNTHETIC:
        await authorize(
            Action.CONNECTOR_NAMED_IMPLEMENT,
            _user_context(cmd),
            Resource(tenant_id=cmd.tenant_id, kind="connector"),
        )

    now = clock.now()
    connector = Connector.draft(
        tenant_id=cmd.tenant_id,
        key=cmd.key,
        kind=cmd.kind,
        design_summary=cmd.design_summary,
        created_by=cmd.actor_id,
        now=now,
        adapter_ref=DEFAULT_SYNTHETIC_ADAPTER_REF,
        # Only recorded for named connectors. The synthetic kind
        # leaves the field None.
        implementation_authorized_by=(
            cmd.actor_id if cmd.kind is not ConnectorKind.SYNTHETIC else None
        ),
        implementation_authorized_at=(now if cmd.kind is not ConnectorKind.SYNTHETIC else None),
        authorization_rationale=cmd.authorization_rationale,
    )
    await connector_repo.add(connector)
    await _emit_audit(
        audit,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        subject_id=connector.id,
        event_type=EVENT_CONNECTOR_DRAFTED,
        payload={
            "key": connector.key,
            "kind": connector.kind.value,
            "state": connector.state.value,
            "design_summary": connector.design_summary,
            "named": connector.is_named(),
        },
        correlation_id=cmd.correlation_id,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_CONNECTOR_DRAFTED,
        event_id=uuid4(),
        payload={
            "connector_id": str(connector.id),
            "tenant_id": str(cmd.tenant_id),
            "key": connector.key,
            "kind": connector.kind.value,
            "state": connector.state.value,
            "occurred_at": now.isoformat(),
        },
    )
    return DraftConnectorResult(connector=connector)


async def approve_connector(
    cmd: ApproveConnectorCommand,
    *,
    connector_repo: Any,
    audit: Any,
    outbox: Any,
    clock: Any,
) -> ApproveConnectorResult:
    """Record an approval (DRAFT → APPROVED).

    Idempotent on already-approved rows. The rationale is required and
    is recorded on the row + audit payload.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="connector"),
    )

    connector = await _load(connector_repo, cmd.tenant_id, cmd.connector_id)
    approved = connector.approve(by=cmd.actor_id, at=clock.now(), rationale=cmd.rationale)
    if approved is connector:
        return ApproveConnectorResult(connector=connector)
    await connector_repo.update(approved)
    now = clock.now()
    await _emit_audit(
        audit,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        subject_id=approved.id,
        event_type=EVENT_CONNECTOR_APPROVED,
        payload={
            "key": approved.key,
            "kind": approved.kind.value,
            "from_state": LIFECYCLE_DRAFT,
            "to_state": approved.state.value,
            "approved_by": str(cmd.actor_id),
            "rationale": cmd.rationale,
            "named": approved.is_named(),
        },
        correlation_id=cmd.correlation_id,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_CONNECTOR_APPROVED,
        event_id=uuid4(),
        payload={
            "connector_id": str(approved.id),
            "tenant_id": str(cmd.tenant_id),
            "key": approved.key,
            "kind": approved.kind.value,
            "from_state": LIFECYCLE_DRAFT,
            "to_state": approved.state.value,
            "occurred_at": now.isoformat(),
        },
    )
    return ApproveConnectorResult(connector=approved)


async def enable_connector(
    cmd: EnableConnectorCommand,
    *,
    connector_repo: Any,
    audit: Any,
    outbox: Any,
    clock: Any,
) -> EnableConnectorResult:
    """Enable an approved connector (APPROVED → ENABLED).

    Idempotent on already-enabled rows. A draining or retired
    connector is refused — the operator must draft a fresh connector
    with a new id if the capability must be revived.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="connector"),
    )

    connector = await _load(connector_repo, cmd.tenant_id, cmd.connector_id)
    enabled = connector.enable(by=cmd.actor_id, at=clock.now())
    if enabled is connector:
        return EnableConnectorResult(connector=connector)
    await connector_repo.update(enabled)
    now = clock.now()
    await _emit_audit(
        audit,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        subject_id=enabled.id,
        event_type=EVENT_CONNECTOR_ENABLED,
        payload={
            "key": enabled.key,
            "kind": enabled.kind.value,
            "from_state": LIFECYCLE_APPROVED,
            "to_state": enabled.state.value,
            "named": enabled.is_named(),
        },
        correlation_id=cmd.correlation_id,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_CONNECTOR_ENABLED,
        event_id=uuid4(),
        payload={
            "connector_id": str(enabled.id),
            "tenant_id": str(cmd.tenant_id),
            "key": enabled.key,
            "kind": enabled.kind.value,
            "from_state": LIFECYCLE_APPROVED,
            "to_state": enabled.state.value,
            "occurred_at": now.isoformat(),
        },
    )
    return EnableConnectorResult(connector=enabled)


async def monitor_connector(
    query: MonitorConnectorQuery,
    *,
    connector_repo: Any,
) -> MonitorConnectorResult:
    """Read the connector's monitor snapshot (no state change)."""
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(query),
        Resource(tenant_id=query.tenant_id, kind="connector"),
    )

    connector = await _load(connector_repo, query.tenant_id, query.connector_id)
    return MonitorConnectorResult(
        connector=connector,
        in_flight=connector.in_flight_count,
        completed=connector.total_completed_count,
        rejected=connector.total_rejected_count,
        accepts_traffic=connector.accepts_traffic(),
        is_terminal=is_terminal_state(connector.state),
    )


async def retire_connector(
    cmd: RetireConnectorCommand,
    *,
    connector_repo: Any,
    audit: Any,
    outbox: Any,
    clock: Any,
) -> RetireConnectorResult:
    """Retire a connector — safe-drain then terminal transition.

    Two sub-transitions:

    1. ``ENABLED → DRAINING``: always issued. New traffic is refused.
       Audit + outbox events are emitted so the operator console can
       show the drain in progress.
    2. ``DRAINING → RETIRED``: issued only when the operator confirms
       the safe-drain invariant (``in_flight == 0``). When the
       operator's snapshot reports in-flight traffic, the use case
       records the confirmation attempt, leaves the connector in
       ``DRAINING``, and returns ``drained=False`` so the HTTP layer
       can surface a 409 and the operator can re-issue the retire
       once the count is zero.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="connector"),
    )

    if cmd.in_flight < 0 or cmd.completed < 0 or cmd.rejected < 0:
        raise DomainValidationFailed(
            "drain counters must be non-negative",
            field="in_flight",
        )

    connector = await _load(connector_repo, cmd.tenant_id, cmd.connector_id)
    now = clock.now()

    # Step 1 — begin the drain (idempotent on already-draining rows).
    draining = connector.begin_drain(by=cmd.actor_id, at=now, notes=cmd.drain_notes)
    drained_changed = draining is not connector
    if drained_changed:
        await connector_repo.update(draining)
        await _emit_audit(
            audit,
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            subject_id=draining.id,
            event_type=EVENT_CONNECTOR_DRAIN_STARTED,
            payload={
                "key": draining.key,
                "kind": draining.kind.value,
                "from_state": LIFECYCLE_ENABLED,
                "to_state": draining.state.value,
                "drain_notes": cmd.drain_notes,
            },
            correlation_id=cmd.correlation_id,
            occurred_at=now,
        )
        await outbox.enqueue(
            tenant_id=cmd.tenant_id,
            event_type=EVENT_CONNECTOR_DRAIN_STARTED,
            event_id=uuid4(),
            payload={
                "connector_id": str(draining.id),
                "tenant_id": str(cmd.tenant_id),
                "key": draining.key,
                "kind": draining.kind.value,
                "from_state": LIFECYCLE_ENABLED,
                "to_state": draining.state.value,
                "occurred_at": now.isoformat(),
            },
        )

    # Step 2 — record the operator's drain confirmation snapshot.
    # The domain's confirm_drain enforces the in-flight==0 invariant
    # before retire() can be called.
    confirmed = draining.confirm_drain(
        in_flight=cmd.in_flight,
        completed=cmd.completed,
        rejected=cmd.rejected,
    )
    await connector_repo.update(confirmed)
    if confirmed.drain_confirmed:
        await _emit_audit(
            audit,
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            subject_id=confirmed.id,
            event_type=EVENT_CONNECTOR_DRAIN_CONFIRMED,
            payload={
                "key": confirmed.key,
                "kind": confirmed.kind.value,
                "in_flight": cmd.in_flight,
                "completed": cmd.completed,
                "rejected": cmd.rejected,
                "drain_confirmed": confirmed.drain_confirmed,
            },
            correlation_id=cmd.correlation_id,
            occurred_at=now,
        )
        await outbox.enqueue(
            tenant_id=cmd.tenant_id,
            event_type=EVENT_CONNECTOR_DRAIN_CONFIRMED,
            event_id=uuid4(),
            payload={
                "connector_id": str(confirmed.id),
                "tenant_id": str(cmd.tenant_id),
                "key": confirmed.key,
                "kind": confirmed.kind.value,
                "in_flight": cmd.in_flight,
                "completed": cmd.completed,
                "rejected": cmd.rejected,
                "drain_confirmed": confirmed.drain_confirmed,
                "occurred_at": now.isoformat(),
            },
        )

    if not confirmed.drain_confirmed or confirmed.in_flight_count > 0:
        # The operator's snapshot reports in-flight traffic. Leave the
        # connector in DRAINING; the operator must re-issue the retire
        # once the count reaches zero.
        return RetireConnectorResult(connector=confirmed, drained=False)

    # Step 3 — terminal transition.
    retired = confirmed.retire(by=cmd.actor_id, at=clock.now())
    if retired is not confirmed:
        await connector_repo.update(retired)
        await _emit_audit(
            audit,
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            subject_id=retired.id,
            event_type=EVENT_CONNECTOR_RETIRED,
            payload={
                "key": retired.key,
                "kind": retired.kind.value,
                "from_state": LIFECYCLE_DRAINING,
                "to_state": retired.state.value,
                "drain_notes": retired.drain_notes,
                "in_flight_at_retire": retired.in_flight_count,
                "completed_at_retire": retired.total_completed_count,
                "rejected_at_retire": retired.total_rejected_count,
            },
            correlation_id=cmd.correlation_id,
            occurred_at=now,
        )
        await outbox.enqueue(
            tenant_id=cmd.tenant_id,
            event_type=EVENT_CONNECTOR_RETIRED,
            event_id=uuid4(),
            payload={
                "connector_id": str(retired.id),
                "tenant_id": str(cmd.tenant_id),
                "key": retired.key,
                "kind": retired.kind.value,
                "from_state": LIFECYCLE_DRAINING,
                "to_state": retired.state.value,
                "occurred_at": now.isoformat(),
            },
        )
    return RetireConnectorResult(connector=retired, drained=True)


__all__ = [
    "approve_connector",
    "draft_connector",
    "enable_connector",
    "monitor_connector",
    "retire_connector",
]
