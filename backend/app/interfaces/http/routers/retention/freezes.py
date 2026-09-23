"""Disposal-freeze routes: declare, cancel, list active (P08-US7).

Registers three endpoints onto the shared :mod:`retention.common` router:

- ``POST /api/v1/admin/disposal-freezes``
- ``POST /api/v1/admin/disposal-freezes/{freeze_id}/cancel``
- ``GET  /api/v1/admin/disposal-freezes/active``

Authorization gates: ``RETENTION_FREEZE_DECLARE`` / ``RETENTION_FREEZE_CANCEL``,
evaluated server-side by the use cases (deny-by-default). A freeze blocks
the disposition pipeline from approving a request inside the window; it
never auto-deletes anything.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.retention.archival import (
    CancelDisposalFreezeCommand,
    DeclareDisposalFreezeCommand,
    cancel_disposal_freeze,
    declare_disposal_freeze,
    list_active_freezes,
)
from app.application.retention.http_dependencies import RetentionHttpDependencyProvider
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.interfaces.http.dependencies import get_retention_dependencies
from app.interfaces.http.schemas.retention import (
    ActiveFreezeListResponse,
    CancelDisposalFreezeRequest,
    DeclareDisposalFreezeRequest,
    DisposalFreezeResponse,
)

from .common import (
    _freeze_repository,
    _freeze_to_response,
    _require_idempotency_key,
    router,
)

# ---------------------------------------------------------------------------
# POST /admin/disposal-freezes
# ---------------------------------------------------------------------------


@router.post(
    "/disposal-freezes",
    response_model=DisposalFreezeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Declare a tenant-wide disposal freeze window (P08-US7)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.freeze_declare",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Idempotency-Key reuse with a different payload",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Missing reason or ends_at <= starts_at",
        },
    },
)
async def declare_disposal_freeze_endpoint(
    body: DeclareDisposalFreezeRequest,
    request: Request,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DisposalFreezeResponse:
    """Declare a tenant-wide freeze window.

    The disposition approval use case refuses approvals while
    any active freeze covers the evaluation moment. The freeze
    does NOT auto-delete anything — it only blocks the
    disposition pipeline from approving a request that is
    inside the window.

    Contracts: ``Idempotency-Key`` is required.
    """
    _require_idempotency_key(idempotency_key)
    cmd = DeclareDisposalFreezeCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        reason=body.reason,
    )
    result = await declare_disposal_freeze(
        cmd,
        freeze_repo=_freeze_repository(request),
        audit=dependencies.for_session(db).audit,
        clock=clock,
    )
    await db.commit()
    return _freeze_to_response(result.freeze)


# ---------------------------------------------------------------------------
# POST /admin/disposal-freezes/{freeze_id}/cancel
# ---------------------------------------------------------------------------


@router.post(
    "/disposal-freezes/{freeze_id}/cancel",
    response_model=DisposalFreezeResponse,
    summary="Cancel an active disposal freeze (P08-US7)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.freeze_cancel",
        },
        status.HTTP_404_NOT_FOUND: {"description": "Freeze not found"},
        status.HTTP_409_CONFLICT: {
            "description": "Already cancelled, or key reuse conflict",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Missing reason",
        },
    },
)
async def cancel_disposal_freeze_endpoint(
    freeze_id: UUID = Path(..., description="Target freeze id (tenant-scoped)"),
    body: CancelDisposalFreezeRequest = ...,
    request: Request = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DisposalFreezeResponse:
    """Cancel an active freeze before its natural ``ends_at`` expires.

    Contracts: ``Idempotency-Key`` is required; cancel-while-cancelled
    surfaces as 409 ``ErrorResponse{conflict}``.
    """
    _require_idempotency_key(idempotency_key)
    cmd = CancelDisposalFreezeCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        freeze_id=freeze_id,
        reason=body.reason,
    )
    result = await cancel_disposal_freeze(
        cmd,
        freeze_repo=_freeze_repository(request),
        audit=dependencies.for_session(db).audit,
        clock=clock,
    )
    await db.commit()
    return _freeze_to_response(result.freeze)


# ---------------------------------------------------------------------------
# GET /admin/disposal-freezes/active
# ---------------------------------------------------------------------------


@router.get(
    "/disposal-freezes/active",
    response_model=ActiveFreezeListResponse,
    summary="List the active disposal freezes (P08-US7)",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.freeze_declare",
        },
    },
)
async def list_active_freezes_endpoint(
    request: Request,
    user: UserContext = Depends(get_current_user),
    clock: Clock = Depends(get_clock),
) -> ActiveFreezeListResponse:
    """Return the active freeze set for the current moment.

    A freeze is active when it has not been cancelled AND the
    current moment is inside the window
    ``[starts_at, ends_at)``. Used by the admin console + the
    disposition approval use case.
    """
    snapshot = await list_active_freezes(
        tenant_id=user.tenant_id,
        freeze_repo=_freeze_repository(request),
        clock=clock,
    )
    return ActiveFreezeListResponse(items=[_freeze_to_response(f) for f in snapshot.freezes])
