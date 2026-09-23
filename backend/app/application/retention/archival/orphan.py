"""Orphan detection scan + proposal review use cases (P08-US8 / FM-041).

:func:`propose_orphan_cases` runs the orphan-detection scan. Cases
past their retention horizon AND with no activity for
:func:`app.domain.retention.ORPHAN_INACTIVITY_DAYS` are surfaced as
:class:`OrphanCaseProposal` rows. The scan never auto-deletes a case
(Constitution IV) — it only stages reviewable proposals, skipping
anything under active legal hold ("hold overrides retention").

:func:`review_orphan_proposal` records the reviewer's decision: a
proposal can be ``surface``d (the disposition approval queue is then
responsible for the next step) or ``dismiss``ed with a required
rationale. Both outcomes are audited in the caller's transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.audit import ORPHAN_PROPOSAL_DISMISSED, ORPHAN_PROPOSAL_SURFACED
from app.domain.errors import Conflict, NotFound, ValidationFailed
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    LegalHoldRepository,
    OrphanCaseProposalRepository,
)
from app.domain.retention import ORPHAN_INACTIVITY_DAYS, OrphanCaseProposal

from .common import (
    SUBJECT_TYPE_ORPHAN_PROPOSAL,
    _ActorProxy,
    _ClockLike,
    _new_correlation_id,
)


@dataclass(frozen=True, slots=True)
class OrphanProposalEntry:
    """A single entry on the orphan-proposal scan result.

    Carries the proposal id + the case id / reference (so the
    records admin sees the human-readable reference) plus the
    last-activity timestamp and the reason the case was
    flagged. The application layer is responsible for
    translating the candidate into a reviewer action; the scan
    itself is read-only with respect to the case data.
    """

    proposal: OrphanCaseProposal
    case_id: UUID
    case_reference: str
    last_activity_at: datetime


@dataclass(frozen=True, slots=True)
class OrphanScanResult:
    """The post-scan outcome returned to the router.

    ``proposals`` are the freshly staged
    :class:`OrphanCaseProposal` rows. ``skipped_holds`` are the
    case ids the scan skipped because they were under active
    legal hold (the pillar's "hold overrides retention"
    invariant is preserved).
    """

    proposals: list[OrphanProposalEntry] = field(default_factory=list)
    skipped_holds: list[UUID] = field(default_factory=list)


async def propose_orphan_cases(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    case_repo: CaseRepository,
    hold_repo: LegalHoldRepository,
    proposal_repo: OrphanCaseProposalRepository,
    clock: _ClockLike,
) -> OrphanScanResult:
    """Run the orphan-detection scan (P08-US8 / FM-041).

    The rule (pillar §2):

    - A case whose :class:`LegalHold` is active is never an
      orphan candidate. The pillar's "hold overrides
      retention" invariant is preserved; the scan simply
      reports the skipped ids so the reviewer can confirm.
    - A case whose most recent activity is older than
      :data:`app.domain.retention.ORPHAN_INACTIVITY_DAYS` is
      eligible. v1 uses ``case.updated_at`` as the activity
      proxy — the case aggregate does not carry a separate
      "last activity" timestamp and the rule is deterministic.
    - A case that already has an open proposal is skipped (a
      case owns at most one open proposal at a time; the
      pre-check via :meth:`get_open_for_case` is the
      authoritative layer).
    - The scan never auto-deletes a case (Constitution IV).
      It only stages reviewable proposals; the reviewer is
      the one who authorizes the next step.

    Failure behavior:

    - Auth errors are raised before any DB write.
    - The scan is idempotent against a re-run: the per-case
      open-proposal uniqueness prevents duplicate rows.
    """
    actor_ctx = _ActorProxy(
        tenant_id=tenant_id,
        user_id=actor_id,
        roles=actor_roles,
    )
    await authorize(
        Action.RETENTION_ORPHAN_SURFACE,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=tenant_id, kind=SUBJECT_TYPE_ORPHAN_PROPOSAL),
    )

    now = clock.now()
    cases = await case_repo.list(tenant_id, limit=200, offset=0)
    proposals: list[OrphanProposalEntry] = []
    skipped_holds: list[UUID] = []
    for case in cases:
        # Pending pre-check: skip cases that already have an open
        # proposal (the per-case uniqueness invariant).
        existing = await proposal_repo.get_open_for_case(tenant_id, case.id)
        if existing is not None:
            continue
        # Legal hold: a held case is never an orphan candidate.
        # The pillar's "hold overrides retention" invariant is
        # preserved; the scan simply reports the skipped id so
        # the reviewer can confirm.
        hold = await hold_repo.get_active(tenant_id, case.id)
        if hold is not None and hold.is_active:
            skipped_holds.append(case.id)
            continue
        # Activity window: a case is an orphan candidate when
        # its last activity is older than
        # ``ORPHAN_INACTIVITY_DAYS``.
        last_activity = case.updated_at
        age = now - last_activity
        if age.days < ORPHAN_INACTIVITY_DAYS:
            continue
        reason = (
            f"no activity in the last {ORPHAN_INACTIVITY_DAYS} days "
            f"(last activity: {last_activity.isoformat()})"
        )
        proposal = OrphanCaseProposal.propose(
            tenant_id=tenant_id,
            case_id=case.id,
            detected_at=now,
            last_activity_at=last_activity,
            reason=reason,
        )
        await proposal_repo.add(proposal)
        proposals.append(
            OrphanProposalEntry(
                proposal=proposal,
                case_id=case.id,
                case_reference=case.reference.value,
                last_activity_at=last_activity,
            )
        )
    return OrphanScanResult(proposals=proposals, skipped_holds=skipped_holds)


@dataclass(frozen=True, slots=True)
class ReviewOrphanProposalCommand:
    """Input to :func:`review_orphan_proposal`.

    ``decision`` is either ``"surface"`` (the proposal is
    accepted and the case is moved into the normal disposition
    pipeline — out of scope for v1, so the audit row records the
    decision only) or ``"dismiss"`` (the proposal is rejected
    with a required ``reason``).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    proposal_id: UUID
    decision: str
    reason: str
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class ReviewOrphanProposalResult:
    """The post-review outcome returned to the router.

    ``proposal`` is the updated :class:`OrphanCaseProposal`
    row (status ``surfaced`` or ``dismissed``).
    """

    proposal: OrphanCaseProposal


async def review_orphan_proposal(
    cmd: ReviewOrphanProposalCommand,
    *,
    proposal_repo: OrphanCaseProposalRepository,
    audit: AuditEventStore,
    clock: _ClockLike,
) -> ReviewOrphanProposalResult:
    """Record a reviewer's decision on an orphan proposal.

    The actual disposition request for a ``surface``d proposal
    is created in a follow-up dispatch (out of scope for v1).
    The audit row carries the decision so the timeline renders
    the review without re-reading the proposal.

    Failure behavior:

    - Auth/not-found errors are raised before any DB write.
    - :class:`NotFound` when the proposal is missing or
      cross-tenant.
    - :class:`Conflict` when the proposal is already decided.
    - :class:`ValidationFailed` when ``decision`` is not
      ``"surface"`` / ``"dismiss"``, or when ``reason`` is
      blank for a dismissal.
    """
    actor_ctx = _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )
    action = (
        Action.RETENTION_ORPHAN_SURFACE
        if cmd.decision == "surface"
        else Action.RETENTION_ORPHAN_DISMISS
    )
    await authorize(
        action,
        actor_ctx,  # type: ignore[arg-type]
        Resource(
            tenant_id=cmd.tenant_id,
            kind=SUBJECT_TYPE_ORPHAN_PROPOSAL,
            id=cmd.proposal_id,
        ),
    )

    proposal = await proposal_repo.get(cmd.tenant_id, cmd.proposal_id)
    if proposal is None:
        raise NotFound(f"orphan proposal {cmd.proposal_id} not found")
    if not proposal.is_open:
        raise Conflict(f"orphan proposal {cmd.proposal_id} is already {proposal.status.value}")

    now = clock.now()
    if cmd.decision == "surface":
        decided = proposal.surface(reviewer_id=cmd.actor_id, at=now)
        event_type = ORPHAN_PROPOSAL_SURFACED
    elif cmd.decision == "dismiss":
        decided = proposal.dismiss(reviewer_id=cmd.actor_id, at=now, reason=cmd.reason)
        event_type = ORPHAN_PROPOSAL_DISMISSED
    else:
        raise ValidationFailed(
            f"invalid decision: {cmd.decision!r} (expected 'surface' or 'dismiss')",
            field="decision",
        )
    await proposal_repo.update(decided)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=event_type,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_ORPHAN_PROPOSAL,
        subject_id=decided.id,
        case_id=decided.case_id,
        correlation_id=cmd.correlation_id or _new_correlation_id(),
        causation_id=None,
        payload={
            "decision": cmd.decision,
            "case_id": str(decided.case_id),
            **decided.to_payload(),
        },
        occurred_at=now,
    )

    return ReviewOrphanProposalResult(proposal=decided)


__all__ = [
    "OrphanProposalEntry",
    "OrphanScanResult",
    "ReviewOrphanProposalCommand",
    "ReviewOrphanProposalResult",
    "propose_orphan_cases",
    "review_orphan_proposal",
]
