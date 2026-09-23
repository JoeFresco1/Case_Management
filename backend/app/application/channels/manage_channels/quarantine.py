"""Quarantine lifecycle use cases: capture, inspect, release, reject (P04 D4 / FM-027).

``quarantine_capture`` enforces the pillar P04 §6 D4 criterion that a single
bad row never blocks the good ones: each captured row is routed through
``app.domain.channel.evaluate_row`` in isolation, so a failure produces a
:class:`QuarantineRecord` for that row while sibling rows in the same batch
continue. The operator console queries :func:`inspect_quarantine` to find the
held row, then either :func:`release_quarantine` (re-process with a
correction) or :func:`reject_quarantine` (drop permanently).

``inspect`` / ``release`` / ``reject`` require ``ADMIN_TENANT_CONFIG`` —
quarantine resolution is an operator-only action; ``quarantine_capture`` is
the pipeline entry point and is gated by the calling capture adapter, not by
the admin policy. Records carry ``source_label`` and ``channel_key`` so the
operator UI can render the queue without ever embedding the row's payload
data (Constitution V) — the diagnostic ``context`` holds keys and the
payload hash, never values.

Split out of the former single-file ``manage_channels.py`` during the R2
package split; channel enable / disable lives in :mod:`.admin`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.channel import (
    PolicyDecision,
    QuarantineRecord,
    QuarantineStatus,
    ValidationSeverity,
    evaluate_row,
)
from app.domain.errors import NotFound
from app.domain.errors import ValidationFailed as DomainValidationFailed
from app.domain.ports import (
    AuditEventStore,
    ChannelRepository,
    OutboxWriter,
    QuarantineRepository,
)

from .commands import (
    InspectQuarantineQuery,
    InspectQuarantineResult,
    QuarantineCaptureCommand,
    QuarantineCaptureResult,
    RejectQuarantineCommand,
    RejectResult,
    ReleaseQuarantineCommand,
    ReleaseResult,
)
from .constants import (
    EVENT_CAPTURE_QUARANTINED,
    EVENT_CAPTURE_REJECTED,
    EVENT_QUARANTINE_REJECTED,
    EVENT_QUARANTINE_RELEASED,
)
from .helpers import _emit_audit, _payload_hash_from_context, _poison_context, _user_context


async def quarantine_capture(
    cmd: QuarantineCaptureCommand,
    *,
    channel_repo: ChannelRepository,
    quarantine_repo: QuarantineRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> QuarantineCaptureResult:
    """Route a single captured row through the channel's policy.

    A disabled channel refuses the row with a synthesized ``REJECT``
    verdict (no ``QuarantineRecord`` is created — the operator has
    already chosen to take it offline). A row that fails the policy is
    quarantined into a fresh ``QuarantineRecord`` whose ``context``
    carries the diagnostic keys (e.g. ``{"missing": ["title"]}``) plus
    ``payload_hash`` (SHA-256 over the canonical row bytes) and
    ``source_system`` (the channel kind). Values themselves are never
    stored — only the hash — so the holding area stays queryable
    without leaking PII (Constitution V).

    External mailbox / scan / OCR engines stay behind injected
    Protocols; this use case imports only the pure
    :func:`app.domain.provenance.compute_payload_hash` helper, never
    infrastructure adapters or vendor SDKs.
    """

    channel = await channel_repo.get(cmd.tenant_id, cmd.channel_id)
    if channel is None:
        raise NotFound(f"channel {cmd.channel_id} not found in tenant")
    if not channel.enabled:
        # Fail-closed: a disabled channel never accepts new captures,
        # not even for quarantine. The audit + outbox trail still
        # records the rejection so an operator can see the gap.
        now = clock.now()
        decision = PolicyDecision(
            accepted=False,
            error_code="channel_disabled",
            error_message=f"channel {channel.key!r} is disabled",
            severity=ValidationSeverity.REJECT,
        )
        await _emit_audit(
            audit,
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            subject_id=channel.id,
            event_type=EVENT_CAPTURE_REJECTED,
            payload={
                "channel_key": channel.key,
                "error_code": decision.error_code,
                "error_message": decision.error_message,
            },
            correlation_id=cmd.correlation_id,
            occurred_at=now,
        )
        await outbox.enqueue(
            tenant_id=cmd.tenant_id,
            event_type=EVENT_CAPTURE_REJECTED,
            event_id=uuid4(),
            payload={
                "channel_id": str(channel.id),
                "tenant_id": str(cmd.tenant_id),
                "channel_key": channel.key,
                "error_code": decision.error_code,
                "occurred_at": now.isoformat(),
            },
        )
        return QuarantineCaptureResult(decision=decision, record=None, channel=channel)

    decision = evaluate_row(values=cmd.values, policy=channel.policy)
    if decision.accepted:
        # The caller is responsible for whatever "commit" means for
        # the channel — this use case only owns quarantine. We surface
        # the accepted verdict so the caller can proceed.
        return QuarantineCaptureResult(decision=decision, record=None, channel=channel)

    now = clock.now()
    # Why hash before branching: both the quarantine row and the
    # reject-only audit need the same fingerprint so the operator can
    # reconcile "dropped" and "held" items against source payloads.
    # Values are hashed, never stored — only keys + hash + source
    # travel into ``context`` / audit (Constitution V).
    poison_context = _poison_context(
        decision_context=decision.context,
        channel_kind=channel.kind,
        values=cmd.values,
    )
    record = QuarantineRecord(
        id=uuid4(),
        tenant_id=cmd.tenant_id,
        channel_id=channel.id,
        channel_key=channel.key,
        source_label=cmd.source_label,
        captured_at=now,
        captured_by=cmd.actor_id,
        error_code=decision.error_code,
        error_message=decision.error_message,
        correlation_id=cmd.correlation_id,
        status=QuarantineStatus.HELD,
        context=poison_context,
    )
    if decision.severity is ValidationSeverity.QUARANTINE:
        await quarantine_repo.add(record)
        await _emit_audit(
            audit,
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            subject_id=record.id,
            event_type=EVENT_CAPTURE_QUARANTINED,
            payload={
                "channel_id": str(channel.id),
                "channel_key": channel.key,
                "source_label": record.source_label,
                "error_code": record.error_code,
                "error_message": record.error_message,
                "context": record.context,
            },
            correlation_id=cmd.correlation_id,
            occurred_at=now,
        )
        await outbox.enqueue(
            tenant_id=cmd.tenant_id,
            event_type=EVENT_CAPTURE_QUARANTINED,
            event_id=uuid4(),
            payload={
                "quarantine_id": str(record.id),
                "channel_id": str(channel.id),
                "tenant_id": str(cmd.tenant_id),
                "channel_key": channel.key,
                "source_label": record.source_label,
                "error_code": record.error_code,
                "occurred_at": now.isoformat(),
            },
        )
        return QuarantineCaptureResult(decision=decision, record=record, channel=channel)

    # REJECT severity: drop without a quarantine row, audit + outbox only.
    # Why still hash: the operator needs the fingerprint to confirm the
    # dropped payload during reconciliation, even though no holding-area
    # row exists. No values travel — only the hash + safe reason.
    await _emit_audit(
        audit,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        subject_id=channel.id,
        event_type=EVENT_CAPTURE_REJECTED,
        payload={
            "channel_key": channel.key,
            "error_code": decision.error_code,
            "error_message": decision.error_message,
            "context": poison_context,
            "payload_hash": poison_context["payload_hash"],
            "source_system": poison_context["source_system"],
        },
        correlation_id=cmd.correlation_id,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_CAPTURE_REJECTED,
        event_id=uuid4(),
        payload={
            "channel_id": str(channel.id),
            "tenant_id": str(cmd.tenant_id),
            "channel_key": channel.key,
            "error_code": decision.error_code,
            "payload_hash": poison_context["payload_hash"],
            "occurred_at": now.isoformat(),
        },
    )
    return QuarantineCaptureResult(decision=decision, record=None, channel=channel)


async def inspect_quarantine(
    query: InspectQuarantineQuery,
    *,
    quarantine_repo: QuarantineRepository,
) -> InspectQuarantineResult:
    """List ``QuarantineRecord`` rows for the operator console.

    Returns the held rows in capture-descending order; ``status`` and
    ``channel_id`` are optional filters. ``total`` is the count of
    returned rows for the slice — the HTTP layer paginates.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(query),
        Resource(tenant_id=query.tenant_id, kind="quarantine_record"),
    )
    rows = await quarantine_repo.list(
        query.tenant_id,
        channel_id=query.channel_id,
        status=query.status,
        limit=query.limit,
        offset=query.offset,
    )
    items = tuple(rows)
    return InspectQuarantineResult(items=items, total=len(items))


async def release_quarantine(
    cmd: ReleaseQuarantineCommand,
    *,
    quarantine_repo: QuarantineRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> ReleaseResult:
    """Mark a held ``QuarantineRecord`` as released.

    The HTTP / CLI caller is responsible for re-running the
    validation that triggered the quarantine (the channel's policy
    may have changed since the row was captured). This use case only
    transitions the record's status and emits the audit + outbox
    events.
    """
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="quarantine_record"),
    )

    record = await quarantine_repo.get(cmd.tenant_id, cmd.quarantine_id)
    if record is None:
        raise NotFound(f"quarantine record {cmd.quarantine_id} not found in tenant")
    if record.status is QuarantineStatus.RELEASED:
        # Idempotent: a second release returns the existing record
        # without appending a duplicate audit / outbox event.
        return ReleaseResult(record=record)
    if record.status is QuarantineStatus.REJECTED:
        raise DomainValidationFailed(
            f"cannot release a {record.status.value} quarantine record",
            field="status",
        )
    now = clock.now()
    released = record.release(by=cmd.actor_id, at=now, note=cmd.note)
    await quarantine_repo.update(released)
    # Why actor + hash in the payload: the audit row must prove *who*
    # resolved the row, *why*, and *which exact payload* was released,
    # without embedding the payload itself (Constitution V). The
    # ``payload_hash`` passthrough comes from the held record's context
    # so the release references the same fingerprint the capture minted.
    await _emit_audit(
        audit,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        subject_id=released.id,
        event_type=EVENT_QUARANTINE_RELEASED,
        payload={
            "channel_id": str(released.channel_id),
            "channel_key": released.channel_key,
            "source_label": released.source_label,
            "error_code": released.error_code,
            "actor_id": str(cmd.actor_id),
            "resolved_by": str(released.resolved_by) if released.resolved_by else "",
            "note": released.resolution_note or "",
            "resolution_note": released.resolution_note or "",
            "payload_hash": _payload_hash_from_context(released.context),
        },
        correlation_id=cmd.correlation_id,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_QUARANTINE_RELEASED,
        event_id=uuid4(),
        payload={
            "quarantine_id": str(released.id),
            "channel_id": str(released.channel_id),
            "tenant_id": str(cmd.tenant_id),
            "channel_key": released.channel_key,
            "resolved_by": str(released.resolved_by) if released.resolved_by else "",
            "occurred_at": now.isoformat(),
        },
    )
    return ReleaseResult(record=released)


async def reject_quarantine(
    cmd: RejectQuarantineCommand,
    *,
    quarantine_repo: QuarantineRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> RejectResult:
    """Mark a held ``QuarantineRecord`` as rejected. Terminal."""
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="quarantine_record"),
    )

    record = await quarantine_repo.get(cmd.tenant_id, cmd.quarantine_id)
    if record is None:
        raise NotFound(f"quarantine record {cmd.quarantine_id} not found in tenant")
    if record.status is QuarantineStatus.REJECTED:
        return RejectResult(record=record)
    if record.status is QuarantineStatus.RELEASED:
        raise DomainValidationFailed(
            f"cannot reject a {record.status.value} quarantine record",
            field="status",
        )
    now = clock.now()
    rejected = record.reject(by=cmd.actor_id, at=now, note=cmd.note)
    await quarantine_repo.update(rejected)
    # Why actor + hash in the payload: mirrors the release rationale —
    # the audit proves who dropped the row, why, and which fingerprint
    # was dropped, without storing payload bytes.
    await _emit_audit(
        audit,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        subject_id=rejected.id,
        event_type=EVENT_QUARANTINE_REJECTED,
        payload={
            "channel_id": str(rejected.channel_id),
            "channel_key": rejected.channel_key,
            "source_label": rejected.source_label,
            "error_code": rejected.error_code,
            "actor_id": str(cmd.actor_id),
            "resolved_by": str(rejected.resolved_by) if rejected.resolved_by else "",
            "note": rejected.resolution_note or "",
            "resolution_note": rejected.resolution_note or "",
            "payload_hash": _payload_hash_from_context(rejected.context),
        },
        correlation_id=cmd.correlation_id,
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_QUARANTINE_REJECTED,
        event_id=uuid4(),
        payload={
            "quarantine_id": str(rejected.id),
            "channel_id": str(rejected.channel_id),
            "tenant_id": str(cmd.tenant_id),
            "channel_key": rejected.channel_key,
            "resolved_by": str(rejected.resolved_by) if rejected.resolved_by else "",
            "occurred_at": now.isoformat(),
        },
    )
    return RejectResult(record=rejected)


__all__ = [
    "inspect_quarantine",
    "quarantine_capture",
    "reject_quarantine",
    "release_quarantine",
]
