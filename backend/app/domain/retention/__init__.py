"""Retention policy + disposition aggregates (P08-US3, US4 / FM-040).

The retention pillar is split into two tightly related aggregates:

- :class:`RetentionPolicy` — a *versioned* declaration of "how long a
  case / document / audit category is kept before it becomes eligible
  for disposition." A new policy version supersedes the prior one; the
  prior version is retained immutably so an auditor can replay which
  policy was active when a record was created.
- :class:`DispositionRequest` — the per-record review queue. A record
  that crosses its retention horizon (and is not under legal hold) is
  surfaced as a :class:`DispositionRequest`; an approver either
  **approves** the disposition (the record is marked for destruction
  and an audit row records who approved it) or **rejects** it (the
  record stays in place; the rejection is audited with the rationale).

# Why a separate versioned aggregate

The pillar spec (§2) requires that "existing records are evaluated
under the policy that was active when they were created." Storing the
policy as immutable versions lets the disposition use case compute
``due_at = case.created_at + policy.retention_period`` against the
policy version that was effective at ``created_at`` — and then
re-render that computation when a future policy is published without
silently re-eligibling the same record under a new horizon. The
``published_at`` timestamp on each version is the ordering anchor;
there is no separate "active" pointer on this aggregate (a published
policy supersedes the prior one in its *category*, not globally — see
:meth:`RetentionPolicy.is_active_at`).

# Why a separate ``DispositionRequest`` aggregate

Disposition is *workflow*, not configuration. The retention policy
declares the horizon; the disposition request is the durable review
record: who surfaced it, when, against which policy version, and the
final approver / rejecter. Splitting the two keeps the configuration
artefact immutable and small (it's JSON-shaped, version-bumped) while
the workflow row carries mutable lifecycle state (``pending`` ->
``approved`` / ``rejected``) plus actor + reason.

# Legal hold

A case under active legal hold is **never** eligible for disposition
(P08 pillar §2: "Hold-overrides-retention; only an explicit release
of hold allows disposition to proceed."). The refusal lives in the
application layer's :func:`list_eligible_for_disposition` — the
domain aggregate does not see holds directly because the legal-hold
lifecycle is a separate aggregate (FM-039) and the retention aggregate
must not import it. Cross-aggregate invariants belong at the
application boundary.

# INTEGRATION_SEAM

``RetentionPolicyRepository`` and ``DispositionRequestRepository`` are
declared in :mod:`app.domain.ports`. The SQL adapters land in a
follow-up dispatch; this slice ships the domain + application + HTTP
surface against in-memory fakes so the seam is locatable, type-
checkable, and the use cases are fully exercised.
"""

from __future__ import annotations

from app.domain.retention.archival import (
    ArchivalStatus,
    ArchivalTier,
    CaseArchival,
    DisposalFreeze,
    is_freeze_blocking,
)
from app.domain.retention.disposition import (
    DispositionRequest,
    DispositionStatus,
    is_eligible_for_disposition,
)
from app.domain.retention.hold_pause import (
    HoldPeriod,
    effective_due_at,
    is_eligible_with_hold_pause,
    total_hold_pause,
)
from app.domain.retention.orphan import (
    ORPHAN_INACTIVITY_DAYS,
    OrphanCaseProposal,
    OrphanProposalStatus,
)
from app.domain.retention.policy import (
    RetentionCategory,
    RetentionPolicy,
    RetentionPolicyStatus,
)

__all__ = [
    "ArchivalStatus",
    "ArchivalTier",
    "CaseArchival",
    "DisposalFreeze",
    "DispositionRequest",
    "DispositionStatus",
    "HoldPeriod",
    "ORPHAN_INACTIVITY_DAYS",
    "OrphanCaseProposal",
    "OrphanProposalStatus",
    "RetentionCategory",
    "RetentionPolicy",
    "RetentionPolicyStatus",
    "effective_due_at",
    "is_eligible_for_disposition",
    "is_eligible_with_hold_pause",
    "is_freeze_blocking",
    "total_hold_pause",
]
