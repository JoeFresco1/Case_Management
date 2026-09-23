"""Approve or reject a pending disposition request (P08-US4 / FM-040)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.application.retention.disposition.commands import (
    CASE_FROZEN_CODE,
    DISPOSAL_FROZEN_CODE,
    SUBJECT_TYPE_DISPOSITION_REQUEST,
    _ActorProxy,
    _ClockLike,
    _new_correlation_id,
)
from app.domain.audit import DISPOSITION_APPROVED, DISPOSITION_REJECTED
from app.domain.errors import Conflict, NotFound
from app.domain.legal_hold import REFUSED_HOLD_CODE
from app.domain.ports import (
    AuditEventStore,
    CaseArchivalRepository,
    DisposalFreezeRepository,
    DispositionRequestRepository,
    LegalHoldRepository,
)
from app.domain.retention import (
    DispositionRequest,
    DispositionStatus,
    is_freeze_blocking,
)


@dataclass(frozen=True, slots=True)
class DispositionDecisionCommand:
    """Input to :func:`approve_disposition` / :func:`reject_disposition`.

    The same shape for both verbs; the rejection requires a
    non-blank ``reason`` (the aggregate enforces it). The
    ``request_id`` is the disposition request surfaced by
    :func:`route_disposition_for_case`.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    request_id: UUID
    reason: str = ""
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class DispositionDecisionResult:
    """The post-decision outcome returned to the router.

    ``request`` is the updated :class:`DispositionRequest` (status
    ``approved`` or ``rejected``, ``reviewed_by`` /
    ``reviewed_at`` / ``review_reason`` populated).
    """

    request: DispositionRequest


async def approve_disposition(
    cmd: DispositionDecisionCommand,
    *,
    disposition_repo: DispositionRequestRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
    hold_repo: LegalHoldRepository | None = None,
    freeze_repo: DisposalFreezeRepository | None = None,
    archival_repo: CaseArchivalRepository | None = None,
) -> DispositionDecisionResult:
    """Approve a pending disposition request.

    Failure behavior:
    - Auth/not-found errors are raised before any DB write.
    - :class:`NotFound` when the request id is missing or
      cross-tenant.
    - :class:`Conflict` when the request is not pending (already
      decided) — the stale-token guard: a reviewer holding a stale
      view cannot silently overwrite a concurrent decision.
    - :class:`Conflict` with ``refused_hold`` when the case is under
      active legal hold (requires ``hold_repo``).
    - :class:`Conflict` with ``disposal_frozen`` when any active
      disposal freeze covers now (requires ``freeze_repo``).
    - :class:`Conflict` with ``case_frozen`` when the case carries
      an active archival row (requires ``archival_repo``).
    - Gate refusals mutate nothing and append no audit row: the
      request stays ``pending`` so the caller retries after the
      hold/freeze/archive clears (rollback by construction — the
      single transaction commits zero rows).
    - The disposition update and the ``DispositionApproved`` audit
      row share one transaction.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.DISPOSITION_APPROVE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(
            tenant_id=cmd.tenant_id,
            kind=SUBJECT_TYPE_DISPOSITION_REQUEST,
            id=cmd.request_id,
        ),
    )

    request = await disposition_repo.get(cmd.tenant_id, cmd.request_id)
    if request is None:
        raise NotFound(f"disposition request {cmd.request_id} not found")
    if request.status is not DispositionStatus.PENDING:
        raise Conflict(f"disposition request {cmd.request_id} is already {request.status.value}")

    now = clock.now()
    # why: gates run last-before-write so a hold placed (or freeze
    # declared, or archive moved) between route and approve still
    # blocks. Each refusal leaves the request pending with no audit
    # row — the reviewer retries after the blocker clears.
    if hold_repo is not None:
        hold = await hold_repo.get_active(cmd.tenant_id, request.case_id)
        if hold is not None and hold.is_active:
            raise Conflict(
                f"{REFUSED_HOLD_CODE}: case {request.case_id} is under active legal hold "
                f"{hold.id}; disposition approval refused"
            )
    if freeze_repo is not None:
        freezes = await freeze_repo.list_active(cmd.tenant_id, at=now)
        if is_freeze_blocking(freezes=freezes, at=now):
            raise Conflict(
                f"{DISPOSAL_FROZEN_CODE}: an active disposal freeze covers now; "
                "disposition approval refused"
            )
    if archival_repo is not None:
        archival = await archival_repo.get_active_for_case(cmd.tenant_id, request.case_id)
        if archival is not None:
            raise Conflict(
                f"{CASE_FROZEN_CODE}: case {request.case_id} is archived in tier "
                f"{archival.tier.value}; unfreeze before approving disposition"
            )

    decided = request.approve(
        reviewer_id=cmd.actor_id,
        at=now,
        reason=cmd.reason or None,
    )
    await disposition_repo.update(decided)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=DISPOSITION_APPROVED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DISPOSITION_REQUEST,
        subject_id=decided.id,
        case_id=decided.case_id,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=None,
        payload={
            "policy_id": str(decided.policy_id),
            **decided.to_payload(),
        },
        occurred_at=now,
    )

    return DispositionDecisionResult(request=decided)


async def reject_disposition(
    cmd: DispositionDecisionCommand,
    *,
    disposition_repo: DispositionRequestRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
) -> DispositionDecisionResult:
    """Reject a pending disposition request.

    ``cmd.reason`` is required (the aggregate refuses a blank
    reason with :class:`ValidationFailed`). Failure modes mirror
    :func:`approve_disposition`; the audit row type is
    ``DispositionRejected``.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.DISPOSITION_REJECT,
        actor_ctx,  # type: ignore[arg-type]
        Resource(
            tenant_id=cmd.tenant_id,
            kind=SUBJECT_TYPE_DISPOSITION_REQUEST,
            id=cmd.request_id,
        ),
    )

    request = await disposition_repo.get(cmd.tenant_id, cmd.request_id)
    if request is None:
        raise NotFound(f"disposition request {cmd.request_id} not found")
    if request.status is not DispositionStatus.PENDING:
        raise Conflict(f"disposition request {cmd.request_id} is already {request.status.value}")

    now = clock.now()
    decided = request.reject(
        reviewer_id=cmd.actor_id,
        at=now,
        reason=cmd.reason,
    )
    await disposition_repo.update(decided)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=DISPOSITION_REJECTED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DISPOSITION_REQUEST,
        subject_id=decided.id,
        case_id=decided.case_id,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=None,
        payload={
            "policy_id": str(decided.policy_id),
            **decided.to_payload(),
        },
        occurred_at=now,
    )

    return DispositionDecisionResult(request=decided)
