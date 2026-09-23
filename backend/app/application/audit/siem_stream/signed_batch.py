"""Signed-batch use cases: build tamper-evident audit spans (P09-US9 / FM-046).

# Signed batch invariant (P09-US9)

A signed batch covers a contiguous range of durable ``audit_events``
rows. The chain is verified end-to-end before signing, so a
misconfigured caller cannot attest a chain that does not exist
on-disk; the signature is the v1 HMAC from
:mod:`app.application.audit.siem_stream.signing`. ``build_signed_batch``
takes an explicit inclusive ``[first_event_id, last_event_id]`` span;
``create_signed_batch`` picks the span from a ``[since, until]``
window and delegates. Both authorize the Gated ``audit.stream`` action
(ADR-009). Split out of the former single ``siem_stream`` module by
the R2 structural refactor; everything here is re-exported from
``app.application.audit.siem_stream`` unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.application.authorization import Action, Policy, Resource, authorize
from app.domain.audit import verify_chain
from app.domain.context import Clock
from app.domain.errors import ValidationFailed
from app.domain.ports import AuditEventStore

from .constants import (
    ALGORITHM_HMAC_SHA256,
    BATCH_KEY_CREATED_AT,
    BATCH_KEY_FIRST_EVENT_ID,
    BATCH_KEY_LAST_EVENT_ID,
    BATCH_KEY_ROW_COUNT,
    BATCH_KEY_TENANT_ID,
    DEFAULT_KEY_ID,
    MAX_BATCH_ROWS,
)
from .helpers import (
    _ActorProxy,
    _new_batch_id,
    _now_utc,
    _row_to_chain,
    _row_to_signed_entry,
)
from .signing import sign_payload
from .types import SignedBatch, SignedBatchRepository


@dataclass(frozen=True, slots=True)
class BuildBatchCommand:
    """The input to :func:`build_signed_batch` (P09-US9 / FM-046).

    ``first_event_id`` and ``last_event_id`` are inclusive
    boundaries. The use case reads the rows in chronological order
    and refuses an out-of-order or non-existent span so a
    misconfigured caller cannot produce a batch that does not
    match the live chain.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    first_event_id: UUID
    last_event_id: UUID
    reason: str
    correlation_id: str


@dataclass(frozen=True, slots=True)
class CreateSignedBatchCommand:
    """The input to :func:`create_signed_batch` (P09-US9 / FM-046).

    Unlike :class:`BuildBatchCommand`, this is for an event-range
    request: ``since`` and ``until`` select the rows. The use case
    picks the first and last events from the result and refuses an
    empty range.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    since: datetime | None
    until: datetime | None
    reason: str
    correlation_id: str
    limit: int = MAX_BATCH_ROWS


def _validate_build(cmd: BuildBatchCommand) -> None:
    """Reject a build request missing the audit-trail fields (422)."""
    if cmd.first_event_id == cmd.last_event_id:
        # A one-row batch is allowed; an inverted span is a 422.
        pass
    if not cmd.reason or not cmd.reason.strip():
        raise ValidationFailed(
            "reason is required for a signed audit batch",
            field="reason",
        )
    if not cmd.correlation_id:
        raise ValidationFailed(
            "correlation_id is required for every audit row",
            field="correlation_id",
        )


def _validate_create(cmd: CreateSignedBatchCommand) -> None:
    """Reject a range request outside the batch caps or missing trail fields (422)."""
    if cmd.limit < 1 or cmd.limit > MAX_BATCH_ROWS:
        raise ValidationFailed(
            f"limit must be between 1 and {MAX_BATCH_ROWS}",
            field="limit",
        )
    if cmd.since is not None and cmd.until is not None and cmd.since > cmd.until:
        raise ValidationFailed(
            "since must be <= until",
            field="since",
        )
    if not cmd.reason or not cmd.reason.strip():
        raise ValidationFailed(
            "reason is required for a signed audit batch",
            field="reason",
        )
    if not cmd.correlation_id:
        raise ValidationFailed(
            "correlation_id is required for every audit row",
            field="correlation_id",
        )


async def _load_range(
    audit: AuditEventStore,
    *,
    tenant_id: UUID,
    first_event_id: UUID,
    last_event_id: UUID,
) -> list[Any]:
    """Load a contiguous span [first_event_id, last_event_id] for ``tenant_id``.

    The span is the inclusive range of ``audit_events`` rows the
    batch covers. We pull a tenant-wide page in chronological order
    and slice by id so the chain is contiguous in one call. The
    caller passes ``first_event_id`` and ``last_event_id`` from
    prior reads (the timeline or the export).
    """

    page_size = MAX_BATCH_ROWS + 1  # defensive headroom
    rows = list(
        await audit.list_for_export(
            tenant_id=tenant_id,
            case_id=None,
            since=None,
            until=None,
            limit=page_size,
            cursor=None,
        )
    )
    start_idx: int | None = None
    end_idx: int | None = None
    for idx, row in enumerate(rows):
        if row.id == first_event_id:
            start_idx = idx
        if row.id == last_event_id:
            end_idx = idx
            break
    if start_idx is None or end_idx is None or start_idx > end_idx:
        raise ValidationFailed(
            "first_event_id must precede last_event_id and both must exist",
            field="first_event_id",
        )
    return rows[start_idx : end_idx + 1]


async def build_signed_batch(
    cmd: BuildBatchCommand,
    *,
    audit: AuditEventStore,
    repository: SignedBatchRepository,
    clock: Clock | None = None,
    policy: Policy | None = None,
    synthetic_secret: str = "synthetic-environment-secret",  # noqa: S107 - synthetic env marker only
) -> SignedBatch:
    """Build a signed batch over the inclusive span [first, last] (P09-US9).

    The chain is verified against the durable rows before signing;
    a mismatch raises ``ValidationFailed`` so a misconfigured caller
    cannot attest a chain that does not exist on-disk. The batch is
    persisted via ``repository`` so the same response can be re-read
    later.
    """

    _validate_build(cmd)

    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.AUDIT_STREAM,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="tenant"),
        policy=policy,
    )

    rows = await _load_range(
        audit,
        tenant_id=cmd.tenant_id,
        first_event_id=cmd.first_event_id,
        last_event_id=cmd.last_event_id,
    )
    if not rows:
        raise ValidationFailed(
            "span is empty",
            field="first_event_id",
        )
    if len(rows) > MAX_BATCH_ROWS:
        raise ValidationFailed(
            f"span exceeds {MAX_BATCH_ROWS} rows; create multiple smaller batches",
            field="first_event_id",
        )

    # Verify the chain end-to-end so a corrupted range is rejected.
    chain_rows = [_row_to_chain(r) for r in rows]
    verification = verify_chain(chain_rows)
    if not verification.ok:
        raise ValidationFailed(
            f"chain verification failed at {verification.first_mismatch_id}",
            field="first_event_id",
        )

    batch_id = _new_batch_id()
    now = clock.now() if clock is not None and hasattr(clock, "now") else _now_utc()
    entries = [_row_to_signed_entry(r) for r in rows]
    payload: dict[str, Any] = {
        BATCH_KEY_TENANT_ID: str(cmd.tenant_id),
        BATCH_KEY_FIRST_EVENT_ID: str(rows[0].id),
        BATCH_KEY_LAST_EVENT_ID: str(rows[-1].id),
        BATCH_KEY_ROW_COUNT: len(rows),
        BATCH_KEY_CREATED_AT: now.isoformat(),
        "reason": cmd.reason,
        "correlation_id": cmd.correlation_id,
        "rows": entries,
    }
    signature = sign_payload(
        tenant_id=cmd.tenant_id,
        payload=payload,
        key_id=DEFAULT_KEY_ID,
        synthetic_secret=synthetic_secret,
    )

    batch = SignedBatch(
        id=batch_id,
        tenant_id=cmd.tenant_id,
        first_event_id=rows[0].id,
        last_event_id=rows[-1].id,
        first_event_hash=rows[0].hash,
        last_event_hash=rows[-1].hash,
        row_count=len(rows),
        algorithm=ALGORITHM_HMAC_SHA256,
        key_id=DEFAULT_KEY_ID,
        signature=signature,
        payload=payload,
        created_at=now,
        created_by=cmd.actor_id,
    )
    await repository.add(batch)
    return batch


async def create_signed_batch(
    cmd: CreateSignedBatchCommand,
    *,
    audit: AuditEventStore,
    repository: SignedBatchRepository,
    clock: Clock | None = None,
    policy: Policy | None = None,
    synthetic_secret: str = "synthetic-environment-secret",  # noqa: S107 - synthetic env marker only
) -> SignedBatch:
    """Build a signed batch over ``[since, until]`` (P09-US9).

    The use case reads the durable rows in chronological order, picks
    the first and last, and delegates to :func:`build_signed_batch`.
    An empty range raises ``ValidationFailed``.
    """

    _validate_create(cmd)

    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.AUDIT_STREAM,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="tenant"),
        policy=policy,
    )

    rows = list(
        await audit.list_for_export(
            tenant_id=cmd.tenant_id,
            case_id=None,
            since=cmd.since,
            until=cmd.until,
            limit=cmd.limit,
            cursor=None,
        )
    )
    if not rows:
        raise ValidationFailed(
            "no audit rows in the requested window",
            field="since",
        )
    return await build_signed_batch(
        BuildBatchCommand(
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            actor_roles=cmd.actor_roles,
            first_event_id=rows[0].id,
            last_event_id=rows[-1].id,
            reason=cmd.reason,
            correlation_id=cmd.correlation_id,
        ),
        audit=audit,
        repository=repository,
        clock=clock,
        policy=policy,
        synthetic_secret=synthetic_secret,
    )


__all__ = [
    "BuildBatchCommand",
    "CreateSignedBatchCommand",
    "_load_range",
    "_validate_build",
    "_validate_create",
    "build_signed_batch",
    "create_signed_batch",
    "verify_chain",
]
