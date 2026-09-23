"""Route an eligible case into the disposition approval queue (P08-US4)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.application.retention.disposition.commands import (
    SUBJECT_TYPE_CASE,
    SUBJECT_TYPE_DISPOSITION_REQUEST,
    _ActorProxy,
    _ClockLike,
    _hold_periods_for_case,
    _new_correlation_id,
)
from app.domain.audit import DISPOSITION_REQUESTED
from app.domain.errors import Conflict, NotFound, ValidationFailed
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    DispositionRequestRepository,
    LegalHoldRepository,
    RetentionPolicyRepository,
)
from app.domain.retention import (
    DispositionRequest,
    RetentionCategory,
    RetentionPolicy,
    effective_due_at,
    is_eligible_with_hold_pause,
)


@dataclass(frozen=True, slots=True)
class RouteDispositionCommand:
    """Input to :func:`route_disposition_for_case`.

    The records admin selects a case from the eligible list and
    asks the system to surface it as a pending disposition request.
    The request id is returned so the admin can hand it to an
    approver.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class RouteDispositionResult:
    """The post-route outcome returned to the router.

    ``request`` is the freshly surfaced :class:`DispositionRequest`.
    """

    request: DispositionRequest


async def route_disposition_for_case(
    cmd: RouteDispositionCommand,
    *,
    case_repo: CaseRepository,
    hold_repo: LegalHoldRepository,
    policy_repo: RetentionPolicyRepository,
    disposition_repo: DispositionRequestRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
) -> RouteDispositionResult:
    """Surface a case as a pending disposition request.

    Failure behavior:
    - Auth/not-found errors are raised before any DB write.
    - ``NotFound`` when the case is missing.
    - Refuses to surface a case under active legal hold
      (``legal_hold_active`` -> 409-equivalent at the HTTP boundary).
    - Refuses to surface a case that does not cross the active
      retention horizon (the admin is asking for a review of an
      in-window record — that's a process error, not a missing
      case).
    - The disposition row and the ``DispositionRequested`` audit row
      share one transaction.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.RETENTION_POLICY_PUBLISH,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind=SUBJECT_TYPE_CASE, id=cmd.case_id),
    )

    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")

    # Pending pre-check: a case already in review cannot be surfaced
    # a second time (the unique partial index is the
    # defense-in-depth layer; this lookup is the authoritative one).
    existing = await disposition_repo.get_pending_for_case(cmd.tenant_id, case.id)
    if existing is not None:
        return RouteDispositionResult(request=existing)

    # Legal hold blocks routing — the use case refuses so the admin
    # cannot accidentally queue a held case for disposition.
    hold = await hold_repo.get_active(cmd.tenant_id, case.id)
    if hold is not None and hold.is_active:
        raise Conflict(
            f"case {case.reference.value} is under legal hold; cannot route for disposition"
        )

    # Resolve the active policy for the case-level category.
    policies = await policy_repo.list_published(
        cmd.tenant_id, category=RetentionCategory.CASE.value
    )
    active_policy: RetentionPolicy | None = None
    for p in policies:
        if p.target_key == "":
            active_policy = p
            break
    if active_policy is None:
        raise NotFound("no published retention policy for case category")

    now = clock.now()
    # why: released-hold time shifts the horizon (pause-and-resume)
    # so a case held for 30 days surfaces 30 days later than the
    # legacy ``created_at + period`` date. Active holds were refused
    # above; their open period still extends ``due_at`` for replay.
    periods = await _hold_periods_for_case(hold_repo, cmd.tenant_id, case.id)
    if not is_eligible_with_hold_pause(
        case_created_at=case.created_at,
        policy=active_policy,
        now=now,
        legal_hold_active=False,
        periods=periods,
    ):
        raise ValidationFailed(
            f"case {case.reference.value} is not yet eligible for disposition",
            field="case_id",
        )

    request = DispositionRequest.surface(
        tenant_id=cmd.tenant_id,
        case_id=case.id,
        policy_id=active_policy.id,
        category=RetentionCategory.CASE,
        due_at=effective_due_at(
            case_created_at=case.created_at,
            policy=active_policy,
            periods=periods,
            now=now,
        ),
        now=now,
        surfaced_by=cmd.actor_id,
    )
    await disposition_repo.add(request)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=DISPOSITION_REQUESTED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DISPOSITION_REQUEST,
        subject_id=request.id,
        case_id=case.id,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=None,
        payload={
            "case_reference": case.reference.value,
            "policy_id": str(active_policy.id),
            "policy_version": active_policy.version_number,
            **request.to_payload(),
        },
        occurred_at=now,
    )

    return RouteDispositionResult(request=request)
