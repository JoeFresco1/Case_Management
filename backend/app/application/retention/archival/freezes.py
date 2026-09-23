"""Disposal freeze declaration + cancellation use cases (P08-US7 / FM-041).

:func:`declare_disposal_freeze` opens a tenant-wide freeze window
during which the disposition approval use case refuses to approve
any pending request. :func:`cancel_disposal_freeze` lifts an active
freeze before its natural ``ends_at`` expires. Multiple freezes may
overlap; the approval use case refuses as long as ANY active freeze
covers the evaluation moment (the freeze is the writer; the approval
use case is the reader).

Neither operation deletes anything on its own (Constitution IV) —
the freeze only blocks the disposition pipeline from approving a
request inside the window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.audit import DISPOSAL_FREEZE_CANCELLED, DISPOSAL_FREEZE_DECLARED
from app.domain.errors import Conflict, NotFound
from app.domain.ports import AuditEventStore, DisposalFreezeRepository
from app.domain.retention import DisposalFreeze

from .common import (
    SUBJECT_TYPE_DISPOSAL_FREEZE,
    _ActorProxy,
    _ClockLike,
    _new_correlation_id,
)


@dataclass(frozen=True, slots=True)
class DeclareDisposalFreezeCommand:
    """Input to :func:`declare_disposal_freeze`.

    ``starts_at`` (inclusive) and ``ends_at`` (exclusive) define
    the window during which the disposition approval use case
    refuses to approve any pending request. ``reason`` is
    required.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    starts_at: datetime
    ends_at: datetime
    reason: str
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class DeclareDisposalFreezeResult:
    """The post-declaration outcome returned to the router."""

    freeze: DisposalFreeze


async def declare_disposal_freeze(
    cmd: DeclareDisposalFreezeCommand,
    *,
    freeze_repo: DisposalFreezeRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
) -> DeclareDisposalFreezeResult:
    """Declare a tenant-wide disposal freeze (P08-US7 / FM-041).

    Multiple freezes may overlap; the disposition approval use
    case refuses as long as ANY active freeze covers the
    evaluation moment. The freeze does NOT auto-delete anything
    — it only blocks the disposition pipeline from approving a
    request that is inside the window.

    Failure behavior:

    - Auth errors are raised before any DB write.
    - :class:`ValidationFailed` when ``ends_at <= starts_at``
      (a zero-length window is meaningless; the use case
      refuses it so a misconfigured admin cannot accidentally
      freeze the pipeline forever).
    - The freeze row and the ``DisposalFreezeDeclared`` audit
      row share one transaction.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.RETENTION_FREEZE_DECLARE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind=SUBJECT_TYPE_DISPOSAL_FREEZE),
    )

    now = clock.now()
    freeze = DisposalFreeze.declare(
        tenant_id=cmd.tenant_id,
        starts_at=cmd.starts_at,
        ends_at=cmd.ends_at,
        reason=cmd.reason,
        created_by=cmd.actor_id,
        created_at=now,
    )
    await freeze_repo.add(freeze)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=DISPOSAL_FREEZE_DECLARED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DISPOSAL_FREEZE,
        subject_id=freeze.id,
        case_id=None,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=None,
        payload=freeze.to_payload(),
        occurred_at=now,
    )

    return DeclareDisposalFreezeResult(freeze=freeze)


@dataclass(frozen=True, slots=True)
class CancelDisposalFreezeCommand:
    """Input to :func:`cancel_disposal_freeze`.

    ``reason`` is required (the audit chain must explain why the
    freeze was lifted early; natural expiry via ``ends_at`` is
    self-documenting and does not require a reason).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    freeze_id: UUID
    reason: str
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class CancelDisposalFreezeResult:
    """The post-cancellation outcome returned to the router.

    ``freeze`` is the updated :class:`DisposalFreeze` row
    (carrying ``cancelled_at`` / ``cancelled_by`` /
    ``cancellation_reason``).
    """

    freeze: DisposalFreeze


async def cancel_disposal_freeze(
    cmd: CancelDisposalFreezeCommand,
    *,
    freeze_repo: DisposalFreezeRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
) -> CancelDisposalFreezeResult:
    """Cancel an active freeze before its natural ``ends_at`` expires.

    Failure behavior:

    - Auth/not-found errors are raised before any DB write.
    - :class:`NotFound` when the freeze is missing or
      cross-tenant.
    - :class:`Conflict` when the freeze is already cancelled
      (the use case's :meth:`get` lookup is the authoritative
      pre-check; the repository raises the same
      :class:`Conflict` as a defense-in-depth layer).
    - The freeze update and the ``DisposalFreezeCancelled``
      audit row share one transaction.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.RETENTION_FREEZE_CANCEL,
        actor_ctx,  # type: ignore[arg-type]
        Resource(
            tenant_id=cmd.tenant_id,
            kind=SUBJECT_TYPE_DISPOSAL_FREEZE,
            id=cmd.freeze_id,
        ),
    )

    freeze = await freeze_repo.get(cmd.tenant_id, cmd.freeze_id)
    if freeze is None:
        raise NotFound(f"disposal freeze {cmd.freeze_id} not found")
    if freeze.cancelled_at is not None:
        raise Conflict(f"disposal freeze {cmd.freeze_id} is already cancelled")

    now = clock.now()
    cancelled = freeze.cancel(
        actor_id=cmd.actor_id,
        at=now,
        reason=cmd.reason,
    )
    await freeze_repo.update(cancelled)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=DISPOSAL_FREEZE_CANCELLED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DISPOSAL_FREEZE,
        subject_id=cancelled.id,
        case_id=None,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=None,
        payload=cancelled.to_payload(),
        occurred_at=now,
    )

    return CancelDisposalFreezeResult(freeze=cancelled)


__all__ = [
    "CancelDisposalFreezeCommand",
    "CancelDisposalFreezeResult",
    "DeclareDisposalFreezeCommand",
    "DeclareDisposalFreezeResult",
    "cancel_disposal_freeze",
    "declare_disposal_freeze",
]
