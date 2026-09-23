"""Orphan-proposal routes: scan, list, review (P08-US8).

Registers three endpoints onto the shared :mod:`retention.common` router:

- ``POST /api/v1/admin/orphan-proposals/scan``
- ``GET  /api/v1/admin/orphan-proposals``
- ``POST /api/v1/admin/orphan-proposals/{proposal_id}/review``

Authorization gates: ``RETENTION_ORPHAN_SURFACE`` / ``RETENTION_ORPHAN_DISMISS``,
evaluated server-side by the use cases (deny-by-default). The scan never
auto-deletes a case (Constitution IV) — it only stages reviewable proposals.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.retention.archival import (
    ReviewOrphanProposalCommand,
    propose_orphan_cases,
    review_orphan_proposal,
)
from app.application.retention.http_dependencies import RetentionHttpDependencyProvider
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.interfaces.http.dependencies import get_retention_dependencies
from app.interfaces.http.schemas.retention import (
    OrphanProposalEntryResponse,
    OrphanProposalListResponse,
    OrphanProposalResponse,
    OrphanScanResponse,
    ReviewOrphanProposalRequest,
)

from .common import (
    _hold_repository,
    _orphan_repository,
    _orphan_to_response,
    _require_idempotency_key,
    router,
)

# ---------------------------------------------------------------------------
# POST /admin/orphan-proposals/scan — run the orphan-detection scan
# ---------------------------------------------------------------------------


@router.post(
    "/orphan-proposals/scan",
    response_model=OrphanScanResponse,
    summary="Run the orphan-detection scan (P08-US8)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.orphan_surface",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Idempotency-Key reuse with a different payload",
        },
    },
)
async def propose_orphan_proposals_endpoint(
    request: Request,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OrphanScanResponse:
    """Run the orphan-detection scan.

    The scan never auto-deletes a case (Constitution IV). It
    only stages reviewable :class:`OrphanCaseProposal` rows;
    the reviewer is the one who authorizes the next step. The
    scan is idempotent against a re-run: the per-case
    open-proposal uniqueness prevents duplicate rows.

    Contracts: ``Idempotency-Key`` is required.
    """
    _require_idempotency_key(idempotency_key)
    result = await propose_orphan_cases(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_repo=dependencies.for_session(db).case,
        hold_repo=_hold_repository(request),
        proposal_repo=_orphan_repository(request),
        clock=clock,
    )
    return OrphanScanResponse(
        items=[
            OrphanProposalEntryResponse(
                proposal_id=entry.proposal.id,
                case_id=entry.case_id,
                case_reference=entry.case_reference,
                detected_at=entry.proposal.detected_at,
                last_activity_at=entry.last_activity_at,
                reason=entry.proposal.reason,
                status=entry.proposal.status.value,  # type: ignore[arg-type]
            )
            for entry in result.proposals
        ],
        skipped_holds=result.skipped_holds,
    )


# ---------------------------------------------------------------------------
# GET /admin/orphan-proposals
# ---------------------------------------------------------------------------


@router.get(
    "/orphan-proposals",
    response_model=OrphanProposalListResponse,
    summary="List the open orphan proposals (P08-US8)",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.orphan_surface",
        },
    },
)
async def list_orphan_proposals_endpoint(
    request: Request,
    user: UserContext = Depends(get_current_user),
) -> OrphanProposalListResponse:
    """Return the open proposals queue (oldest first)."""
    proposals = await _orphan_repository(request).list_open(user.tenant_id, limit=200, offset=0)
    return OrphanProposalListResponse(
        items=[
            OrphanProposalEntryResponse(
                proposal_id=p.id,
                case_id=p.case_id,
                case_reference="",  # reference is not on the proposal; UI can resolve
                detected_at=p.detected_at,
                last_activity_at=p.last_activity_at,
                reason=p.reason,
                status=p.status.value,  # type: ignore[arg-type]
            )
            for p in proposals
        ]
    )


# ---------------------------------------------------------------------------
# POST /admin/orphan-proposals/{proposal_id}/review
# ---------------------------------------------------------------------------


@router.post(
    "/orphan-proposals/{proposal_id}/review",
    response_model=OrphanProposalResponse,
    summary="Record a reviewer's decision on an orphan proposal (P08-US8)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.orphan_surface or retention.orphan_dismiss",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Orphan proposal not found",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Already decided, or key reuse conflict",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid decision or missing dismiss reason",
        },
    },
)
async def review_orphan_proposal_endpoint(
    proposal_id: UUID = Path(..., description="Target proposal id (tenant-scoped)"),
    body: ReviewOrphanProposalRequest = ...,
    request: Request = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OrphanProposalResponse:
    """Record a reviewer's decision on an orphan proposal.

    ``decision`` is either ``"surface"`` (the proposal is
    accepted; the case is moved into the normal disposition
    pipeline — out of scope for v1) or ``"dismiss"`` (the
    proposal is rejected with a required ``reason``).

    Contracts: ``Idempotency-Key`` is required; review-while-decided
    surfaces as 409 ``ErrorResponse{conflict}``.
    """
    _require_idempotency_key(idempotency_key)
    cmd = ReviewOrphanProposalCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        proposal_id=proposal_id,
        decision=body.decision,
        reason=body.reason,
    )
    result = await review_orphan_proposal(
        cmd,
        proposal_repo=_orphan_repository(request),
        audit=dependencies.for_session(db).audit,
        clock=clock,
    )
    await db.commit()
    return _orphan_to_response(result.proposal)
