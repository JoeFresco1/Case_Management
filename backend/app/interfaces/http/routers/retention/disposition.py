"""Disposition routes: eligibility scan, routing, approve/reject (P08-US4 / FM-041).

Registers four endpoints onto the shared :mod:`retention.common` router:

- ``GET  /api/v1/admin/disposition/eligible``
- ``POST /api/v1/admin/disposition/eligible/{case_id}/route``
- ``POST /api/v1/admin/disposition/{request_id}/approve``
- ``POST /api/v1/admin/disposition/{request_id}/reject``

Authorization gates: ``RETENTION_POLICY_PUBLISH`` for list/route,
``DISPOSITION_APPROVE`` / ``DISPOSITION_REJECT`` for the decisions —
all evaluated server-side by the use cases (deny-by-default).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.retention.disposition import (
    DispositionDecisionCommand,
    RouteDispositionCommand,
    approve_disposition,
    list_eligible_for_disposition,
    reject_disposition,
    route_disposition_for_case,
)
from app.application.retention.http_dependencies import RetentionHttpDependencyProvider
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.interfaces.http.dependencies import get_retention_dependencies
from app.interfaces.http.schemas.retention import (
    DispositionDecisionRequest,
    DispositionRequestResponse,
    EligibleCaseEntryResponse,
    EligibleListResponse,
)

from .common import (
    _disposition_repository,
    _hold_repository,
    _policy_repository,
    _request_to_response,
    _require_idempotency_key,
    router,
)

# ---------------------------------------------------------------------------
# GET /admin/disposition/eligible — list eligible cases
# ---------------------------------------------------------------------------


@router.get(
    "/disposition/eligible",
    response_model=EligibleListResponse,
    summary="List cases eligible for disposition review",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.policy_publish",
        },
    },
)
async def list_eligible_endpoint(
    request: Request,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
) -> EligibleListResponse:
    """Return cases past their retention horizon, hold-free, and not already pending."""
    result = await list_eligible_for_disposition(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_repo=dependencies.for_session(db).case,
        hold_repo=_hold_repository(request),
        policy_repo=_policy_repository(request),
        disposition_repo=_disposition_repository(request),
        clock=clock,
    )
    return EligibleListResponse(
        items=[
            EligibleCaseEntryResponse(
                case_id=e.case_id,
                case_reference=e.case_reference,
                case_created_at=e.case_created_at,
                policy_id=e.policy_id,
                due_at=e.due_at,
            )
            for e in result.items
        ],
        already_pending=result.already_pending,
    )


# ---------------------------------------------------------------------------
# POST /admin/disposition/eligible/{case_id}/route — surface a request
# ---------------------------------------------------------------------------


@router.post(
    "/disposition/eligible/{case_id}/route",
    response_model=DispositionRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Surface a case as a pending disposition request",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.policy_publish",
        },
        status.HTTP_404_NOT_FOUND: {"description": "Case not found"},
        status.HTTP_409_CONFLICT: {
            "description": "Hold active, already pending, or key reuse conflict",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Case is not yet eligible for disposition",
        },
    },
)
async def route_disposition_endpoint(
    case_id: UUID = Path(..., description="Target case id (tenant-scoped)"),
    request: Request = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DispositionRequestResponse:
    """Route an eligible case into the disposition approval queue.

    Contracts: ``Idempotency-Key`` is required; concurrent route-while-
    pending surfaces as 409 ``ErrorResponse{conflict}``.
    """
    _require_idempotency_key(idempotency_key)
    cmd = RouteDispositionCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=case_id,
    )
    repositories = dependencies.for_session(db)
    result = await route_disposition_for_case(
        cmd,
        case_repo=repositories.case,
        hold_repo=_hold_repository(request),
        policy_repo=_policy_repository(request),
        disposition_repo=_disposition_repository(request),
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return _request_to_response(result.request)


# ---------------------------------------------------------------------------
# POST /admin/disposition/{request_id}/approve | reject
# ---------------------------------------------------------------------------


@router.post(
    "/disposition/{request_id}/approve",
    response_model=DispositionRequestResponse,
    summary="Approve a pending disposition request",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.disposition_approve",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Disposition request not found",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Already decided, or key reuse conflict",
        },
    },
)
async def approve_disposition_endpoint(
    request_id: UUID = Path(..., description="Target disposition request id"),
    body: DispositionDecisionRequest = ...,
    request: Request = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DispositionRequestResponse:
    """Approve a pending disposition request.

    The disposition update and the ``DispositionApproved`` audit row
    share one transaction. Re-approving an already-approved request
    is a no-op (the aggregate is idempotent); the HTTP layer
    surfaces 409 when the request is already decided so the client
    can render an actionable error.

    Contracts: ``Idempotency-Key`` is required; concurrent
    approve-while-decided surfaces as 409 ``ErrorResponse{conflict}``.
    """
    _require_idempotency_key(idempotency_key)
    cmd = DispositionDecisionCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        request_id=request_id,
        reason=body.reason,
    )
    result = await approve_disposition(
        cmd,
        disposition_repo=_disposition_repository(request),
        audit=dependencies.for_session(db).audit,
        clock=clock,
    )
    await db.commit()
    return _request_to_response(result.request)


@router.post(
    "/disposition/{request_id}/reject",
    response_model=DispositionRequestResponse,
    summary="Reject a pending disposition request",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.disposition_reject",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Disposition request not found",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Already decided, or key reuse conflict",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Missing reason",
        },
    },
)
async def reject_disposition_endpoint(
    request_id: UUID = Path(..., description="Target disposition request id"),
    body: DispositionDecisionRequest = ...,
    request: Request = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DispositionRequestResponse:
    """Reject a pending disposition request.

    ``reason`` is required (the domain ``DispositionRequest.reject``
    raises :class:`ValidationFailed` on a blank reason — the HTTP
    layer surfaces it as 422).

    Contracts: ``Idempotency-Key`` is required; concurrent
    reject-while-decided surfaces as 409 ``ErrorResponse{conflict}``.
    """
    _require_idempotency_key(idempotency_key)
    cmd = DispositionDecisionCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        request_id=request_id,
        reason=body.reason,
    )
    result = await reject_disposition(
        cmd,
        disposition_repo=_disposition_repository(request),
        audit=dependencies.for_session(db).audit,
        clock=clock,
    )
    await db.commit()
    return _request_to_response(result.request)
