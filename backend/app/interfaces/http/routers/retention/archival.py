"""Case archival routes: archive + unfreeze (P08-US5).

Registers two endpoints onto the shared :mod:`retention.common` router:

- ``POST /api/v1/admin/archival/cases/{case_id}/archive``
- ``POST /api/v1/admin/archival/cases/{case_id}/unfreeze``

Authorization gates: ``RETENTION_ARCHIVE`` / ``RETENTION_UNFREEZE``,
evaluated server-side by the use cases (deny-by-default). Archiving
freezes the case; the disposition approval use case refuses approvals
while the case is frozen.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.retention.archival import (
    ArchiveCaseCommand,
    UnfreezeCaseCommand,
    archive_case,
    unfreeze_case,
)
from app.application.retention.http_dependencies import RetentionHttpDependencyProvider
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.retention import ArchivalTier
from app.interfaces.http.dependencies import get_retention_dependencies
from app.interfaces.http.schemas.retention import (
    ArchiveCaseRequest,
    CaseArchivalResponse,
    UnfreezeCaseRequest,
)

from .common import (
    _archival_repository,
    _archival_to_response,
    _require_idempotency_key,
    router,
)

# ---------------------------------------------------------------------------
# POST /admin/archival/cases/{case_id}/archive
# ---------------------------------------------------------------------------


@router.post(
    "/archival/cases/{case_id}/archive",
    response_model=CaseArchivalResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Move a case to a cooler storage tier (P08-US5)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.archive",
        },
        status.HTTP_404_NOT_FOUND: {"description": "Case not found"},
        status.HTTP_409_CONFLICT: {
            "description": "Already archived, or key reuse conflict",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Missing reason or invalid tier",
        },
    },
)
async def archive_case_endpoint(
    case_id: UUID = Path(..., description="Target case id (tenant-scoped)"),
    body: ArchiveCaseRequest = ...,
    request: Request = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CaseArchivalResponse:
    """Archive the case in the requested tier.

    The case is frozen: the disposition approval use case refuses
    approvals while the row is in ``archived`` status. The
    archival row and the ``CaseArchived`` audit row share one
    transaction; the unfreeze is a separate, audited action.

    Contracts: ``Idempotency-Key`` is required; concurrent
    archive-while-archived surfaces as 409 ``ErrorResponse{conflict}``.
    """
    _require_idempotency_key(idempotency_key)
    cmd = ArchiveCaseCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=case_id,
        tier=ArchivalTier(body.tier),
        reason=body.reason,
    )
    repositories = dependencies.for_session(db)
    result = await archive_case(
        cmd,
        case_repo=repositories.case,
        archival_repo=_archival_repository(request),
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return _archival_to_response(result.archival)


# ---------------------------------------------------------------------------
# POST /admin/archival/cases/{case_id}/unfreeze
# ---------------------------------------------------------------------------


@router.post(
    "/archival/cases/{case_id}/unfreeze",
    response_model=CaseArchivalResponse,
    summary="Lift the freeze on an archived case (P08-US5)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks retention.unfreeze",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Case not found or not currently archived",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Idempotency-Key reuse with a different payload",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Missing reason",
        },
    },
)
async def unfreeze_case_endpoint(
    case_id: UUID = Path(..., description="Target case id (tenant-scoped)"),
    body: UnfreezeCaseRequest = ...,
    request: Request = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: RetentionHttpDependencyProvider = Depends(get_retention_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CaseArchivalResponse:
    """Lift the freeze on an archived case.

    The historical archival row stays in the audit chain; the
    ``(tenant_id, case_id)`` active slot is vacated so a future
    archive rebinds it.

    Contracts: ``Idempotency-Key`` is required.
    """
    _require_idempotency_key(idempotency_key)
    cmd = UnfreezeCaseCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=case_id,
        reason=body.reason,
    )
    repositories = dependencies.for_session(db)
    result = await unfreeze_case(
        cmd,
        case_repo=repositories.case,
        archival_repo=_archival_repository(request),
        audit=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return _archival_to_response(result.archival)
