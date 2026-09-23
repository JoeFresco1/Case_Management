"""Shared router, idempotency guard, projections, and repository factories.

# Why this module

The retention router (P08-US3..US8 / FM-040, FM-041) was a single ~1100-line
module — past the R2 soft limit of 600 lines. This module holds the pieces
every route group needs:

- :data:`router` — the one ``APIRouter(prefix="/admin")`` that the route
  modules (:mod:`policies`, :mod:`disposition`, :mod:`archival`,
  :mod:`freezes`, :mod:`orphans`) register onto. Registration order is
  irrelevant: no two retention paths overlap at the same segment depth and
  method, so Starlette's first-match rule resolves identically regardless
  of import order.
- :func:`_require_idempotency_key` — router-boundary ``Idempotency-Key``
  contract (400 before the use case; see the package docstring for the
  two-layer idempotency design).
- the six ``app.state`` repository factories — the H1 in-memory seam
  (AGENTS.md §12); SQL adapters replace them behind the same domain ports
  without touching any route module.
- the domain → wire response projections.

# INTEGRATION_SEAM: records stores

The factory functions below read process-scoped repositories off
``app.state``. The local adapters are the ``InMemory*Repository`` classes
under ``app.infrastructure.persistence.retention_repository``; Azure-side
adapters plug in by rebinding ``app.state`` at composition time
(``app.main.create_app``) — no router change.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, HTTPException, Request, status

from app.domain.ports import (
    CaseArchivalRepository,
    DisposalFreezeRepository,
    DispositionRequestRepository,
    LegalHoldRepository,
    OrphanCaseProposalRepository,
    RetentionPolicyRepository,
)
from app.domain.retention import (
    CaseArchival,
    DisposalFreeze,
    RetentionPolicy,
)
from app.interfaces.http.schemas.retention import (
    CaseArchivalResponse,
    DisposalFreezeResponse,
    DispositionRequestResponse,
    OrphanProposalResponse,
    RetentionPolicyResponse,
)

router = APIRouter(prefix="/admin", tags=["admin", "retention"])


def _require_idempotency_key(value: str | None) -> str:
    """Enforce the ``Idempotency-Key`` contract at the router boundary.

    The global :class:`IdempotencyMiddleware` replays transport retries,
    but router-level enforcement keeps the contract explicit in OpenAPI
    and deterministic under test clients that disable the middleware.
    A missing/blank key is 400 so the mutation never reaches the use case.

    Args:
        value: Raw ``Idempotency-Key`` header value (or ``None``).

    Returns:
        The stripped key.

    Raises:
        HTTPException: 400 when the key is missing, blank, or over 128
            characters.
    """
    if not value or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )
    if len(value.strip()) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key is too long",
        )
    return value.strip()


# ---------------------------------------------------------------------------
# Response projections
# ---------------------------------------------------------------------------


def _policy_to_response(policy: RetentionPolicy) -> RetentionPolicyResponse:
    """Project the domain ``RetentionPolicy`` onto the wire response shape."""
    return RetentionPolicyResponse(
        id=policy.id,
        tenant_id=policy.tenant_id,
        category=policy.category.value,  # type: ignore[arg-type]
        target_key=policy.target_key,
        period_days=int(policy.period.total_seconds() // 86400),
        version_number=policy.version_number,
        status=policy.status.value,  # type: ignore[arg-type]
        published_at=policy.published_at,
        published_by=policy.published_by,
        created_at=policy.created_at,
    )


def _request_to_response(
    req,  # type: ignore[no-untyped-def]
) -> DispositionRequestResponse:
    """Project the domain ``DispositionRequest`` onto the wire shape."""
    return DispositionRequestResponse(
        id=req.id,
        tenant_id=req.tenant_id,
        case_id=req.case_id,
        policy_id=req.policy_id,
        category=req.category.value,  # type: ignore[arg-type]
        due_at=req.due_at,
        status=req.status.value,  # type: ignore[arg-type]
        surfaced_at=req.surfaced_at,
        surfaced_by=req.surfaced_by,
        reviewed_by=req.reviewed_by,
        reviewed_at=req.reviewed_at,
        review_reason=req.review_reason,
    )


def _archival_to_response(row: CaseArchival) -> CaseArchivalResponse:
    """Project the domain ``CaseArchival`` onto the wire shape."""
    return CaseArchivalResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        case_id=row.case_id,
        tier=row.tier.value,  # type: ignore[arg-type]
        archived_at=row.archived_at,
        archived_by=row.archived_by,
        reason=row.reason,
        status=row.status.value,  # type: ignore[arg-type]
        unfrozen_at=row.unfrozen_at,
        unfrozen_by=row.unfrozen_by,
        unfreeze_reason=row.unfreeze_reason,
    )


def _freeze_to_response(freeze: DisposalFreeze) -> DisposalFreezeResponse:
    """Project the domain ``DisposalFreeze`` onto the wire shape."""
    return DisposalFreezeResponse(
        id=freeze.id,
        tenant_id=freeze.tenant_id,
        starts_at=freeze.starts_at,
        ends_at=freeze.ends_at,
        reason=freeze.reason,
        created_at=freeze.created_at,
        created_by=freeze.created_by,
        cancelled_at=freeze.cancelled_at,
        cancelled_by=freeze.cancelled_by,
        cancellation_reason=freeze.cancellation_reason,
    )


def _orphan_to_response(
    proposal,  # type: ignore[no-untyped-def]
) -> OrphanProposalResponse:
    """Project the domain ``OrphanCaseProposal`` onto the wire shape."""
    return OrphanProposalResponse(
        id=proposal.id,
        tenant_id=proposal.tenant_id,
        case_id=proposal.case_id,
        detected_at=proposal.detected_at,
        last_activity_at=proposal.last_activity_at,
        reason=proposal.reason,
        status=proposal.status.value,  # type: ignore[arg-type]
        reviewed_at=proposal.reviewed_at,
        reviewed_by=proposal.reviewed_by,
        review_reason=proposal.review_reason,
    )


# ---------------------------------------------------------------------------
# Provider + repository factories
# ---------------------------------------------------------------------------


def _policy_repository(request: Request) -> RetentionPolicyRepository:
    """Return the process-scoped policy repository from ``app.state``.

    The router depends only on the ``RetentionPolicyRepository`` domain
    port so a future SQL adapter swaps in transparently.

    Args:
        request: Current HTTP request (used to reach ``app.state``).

    Returns:
        The configured repository.

    Raises:
        RuntimeError: if an app was created without the shared repository.
    """
    repo = getattr(request.app.state, "retention_policy_repository", None)
    if repo is None:
        raise RuntimeError("retention policy repository is not configured")
    return cast(RetentionPolicyRepository, repo)


def _disposition_repository(request: Request) -> DispositionRequestRepository:
    """Return the process-scoped disposition repository from ``app.state``.

    Args:
        request: Current HTTP request (used to reach ``app.state``).

    Returns:
        The configured repository.

    Raises:
        RuntimeError: if an app was created without the shared repository.
    """
    repo = getattr(request.app.state, "disposition_repository", None)
    if repo is None:
        raise RuntimeError("disposition repository is not configured")
    return cast(DispositionRequestRepository, repo)


def _hold_repository(request: Request) -> LegalHoldRepository:
    """Return the shared legal-hold repository from ``app.state``.

    The disposition router must consult the legal-hold state on the
    eligibility scan; it shares the same store the holds
    router mutates so a freshly-placed hold immediately disqualifies
    the case from disposition.

    Args:
        request: Current HTTP request (used to reach ``app.state``).

    Returns:
        The configured repository.

    Raises:
        RuntimeError: if an app was created without the shared repository.
    """
    repo = getattr(request.app.state, "legal_hold_repository", None)
    if repo is None:
        raise RuntimeError("legal hold repository is not configured")
    return cast(LegalHoldRepository, repo)


def _archival_repository(request: Request) -> CaseArchivalRepository:
    """Return the process-scoped archival repository from ``app.state``.

    Args:
        request: Current HTTP request (used to reach ``app.state``).

    Returns:
        The configured repository.

    Raises:
        RuntimeError: if an app was created without the shared repository.
    """
    repo = getattr(request.app.state, "case_archival_repository", None)
    if repo is None:
        raise RuntimeError("case archival repository is not configured")
    return cast(CaseArchivalRepository, repo)


def _freeze_repository(request: Request) -> DisposalFreezeRepository:
    """Return the process-scoped freeze repository from ``app.state``.

    Args:
        request: Current HTTP request (used to reach ``app.state``).

    Returns:
        The configured repository.

    Raises:
        RuntimeError: if an app was created without the shared repository.
    """
    repo = getattr(request.app.state, "disposal_freeze_repository", None)
    if repo is None:
        raise RuntimeError("disposal freeze repository is not configured")
    return cast(DisposalFreezeRepository, repo)


def _orphan_repository(request: Request) -> OrphanCaseProposalRepository:
    """Return the process-scoped orphan-proposal repository from ``app.state``.

    Args:
        request: Current HTTP request (used to reach ``app.state``).

    Returns:
        The configured repository.

    Raises:
        RuntimeError: if an app was created without the shared repository.
    """
    repo = getattr(request.app.state, "orphan_proposal_repository", None)
    if repo is None:
        raise RuntimeError("orphan proposal repository is not configured")
    return cast(OrphanCaseProposalRepository, repo)
