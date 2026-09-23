"""List the cases that are eligible for disposition review (P08-US4 / FM-040)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.application.retention.disposition.commands import (
    _ActorProxy,
    _ClockLike,
    _hold_periods_for_case,
)
from app.domain.ports import (
    CaseRepository,
    DispositionRequestRepository,
    LegalHoldRepository,
    RetentionPolicyRepository,
)
from app.domain.retention import (
    RetentionCategory,
    RetentionPolicy,
    effective_due_at,
    is_eligible_with_hold_pause,
)


@dataclass(frozen=True, slots=True)
class EligibleCaseEntry:
    """A single eligible-case entry on the list response.

    Carries the case id + reference (so the records admin sees the
    human-readable reference) plus the policy id that declared the
    horizon and the computed ``due_at`` (case.created_at + period).
    The application layer is responsible for translating the
    candidate into a :class:`DispositionRequest` once the admin
    chooses to "route to the approval queue" (the disposition use
    case ships both the list and the route-to-approval behavior).
    """

    case_id: UUID
    case_reference: str
    case_created_at: datetime
    policy_id: UUID
    due_at: datetime


@dataclass(frozen=True, slots=True)
class EligibleListResult:
    """The post-list outcome returned to the router.

    ``items`` are the eligible case entries the admin can route
    into the approval queue. ``already_pending`` is the set of case
    ids that already have a pending disposition request — they are
    excluded from ``items`` but reported so the UI can render a
    "already in review" hint.
    """

    items: list[EligibleCaseEntry] = field(default_factory=list)
    already_pending: list[UUID] = field(default_factory=list)


async def list_eligible_for_disposition(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    case_repo: CaseRepository,
    hold_repo: LegalHoldRepository,
    policy_repo: RetentionPolicyRepository,
    disposition_repo: DispositionRequestRepository,
    clock: _ClockLike,
) -> EligibleListResult:
    """Return the cases that are eligible for disposition review.

    The eligibility rule is delegated to the pure function
    :func:`app.domain.retention.is_eligible_for_disposition`; this
    function is the orchestration: it iterates the published
    policies, resolves the case list per policy target, and skips
    cases under legal hold or with an existing pending request.

    Authorization: the records-admin role grants
    ``RETENTION_POLICY_PUBLISH`` which is the closest existing
    action; eligibility listing is a read-only view that surfaces
    the same data the disposition approval does. Future slices may
    split the action (FM-040 follow-up); for v1 the records admin's
    existing grant covers both.
    """
    actor_ctx = _ActorProxy(
        tenant_id=tenant_id,
        user_id=actor_id,
        roles=actor_roles,
    )
    await authorize(
        Action.RETENTION_POLICY_PUBLISH,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=tenant_id, kind="retention"),
    )

    now = clock.now()
    # v1: only the case-level category. Document / audit categories
    # are deferred to the follow-up slice (FM-040 P3).
    policies = await policy_repo.list_published(tenant_id, category=RetentionCategory.CASE.value)
    if not policies:
        return EligibleListResult()

    # The case-level policy carries the empty ``target_key``; resolve
    # it. The eligibility scan only inspects the most recently
    # published policy per (category, target_key) — a prior policy
    # supersedes only after the new one is published (the repository
    # returns ``published_at DESC`` so the first match wins).
    active_policy: RetentionPolicy | None = None
    for p in policies:
        if p.target_key == "":
            active_policy = p
            break
    if active_policy is None:
        return EligibleListResult()

    cases = await case_repo.list(tenant_id, limit=200, offset=0)
    eligible: list[EligibleCaseEntry] = []
    pending: list[UUID] = []
    for case in cases:
        # Pending pre-check: skip cases that already have a request.
        existing = await disposition_repo.get_pending_for_case(tenant_id, case.id)
        if existing is not None:
            pending.append(case.id)
            continue
        # Legal hold lookup: a held case is never eligible (frozen
        # clock — hold time pauses the horizon via ``periods`` below
        # once the hold is released).
        hold = await hold_repo.get_active(tenant_id, case.id)
        legal_hold_active = hold is not None and hold.is_active
        periods = await _hold_periods_for_case(hold_repo, tenant_id, case.id)
        if not is_eligible_with_hold_pause(
            case_created_at=case.created_at,
            policy=active_policy,
            now=now,
            legal_hold_active=legal_hold_active,
            periods=periods,
        ):
            continue
        eligible.append(
            EligibleCaseEntry(
                case_id=case.id,
                case_reference=case.reference.value,
                case_created_at=case.created_at,
                policy_id=active_policy.id,
                due_at=effective_due_at(
                    case_created_at=case.created_at,
                    policy=active_policy,
                    periods=periods,
                    now=now,
                ),
            )
        )
    return EligibleListResult(items=eligible, already_pending=pending)
