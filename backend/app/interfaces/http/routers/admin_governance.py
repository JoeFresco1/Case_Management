"""Admin governance API (FM-021 / P10-US4, US5, US8).

Endpoints:

- ``GET /api/v1/admin/publications`` — publication history timeline.
  Optional ``artifact_type`` and ``artifact_id`` query parameters
  scope the result to a single artifact (the admin UI's per-version
  tab); without those, the result is the tenant-wide timeline.
- ``POST /api/v1/admin/publications`` — append a publication row
  (called by the publish / retire flows). Returns ``approval_required``
  in the body when the tenant's policy mandates second-admin approval.
- ``POST /api/v1/admin/publications/approve`` — record a second
  administrator's approval (P10-US8). Enforces separation-of-duties
  at the use-case layer.

# Authorization

All endpoints require ``Action.ADMIN_TENANT_CONFIG``. The fail-closed
posture is the same as the case-type admin router
(``interfaces/http/routers/admin_case_types.py``).

# Transaction shape

The router relies on the standard ``get_db`` dependency to commit the
session after the use case returns. The publication row + the
second-admin ``validate`` row are written by the SQL repository in the
same transaction as the status flip on the version row when the
publish use case is wired to call
:func:`governance.record_publication` (the current slice lands the
HTTP surface; the wiring into the publish use case is a follow-up
edit the caller can do without a domain change).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.configuration import governance
from app.application.configuration.http_dependencies import (
    AdminGovernanceHttpDependencyProvider,
)
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.interfaces.http.dependencies import get_admin_governance_dependencies
from app.interfaces.http.schemas.admin_governance import (
    ApprovePublicationRequest,
    ApprovePublicationResponse,
    PublicationHistoryResponse,
    PublicationRecordResponse,
    RecordPublicationRequest,
    RecordPublicationResponse,
)

router = APIRouter(prefix="/admin/publications", tags=["admin", "configuration"])


def _to_response(record: governance.PublicationRecord) -> PublicationRecordResponse:
    return PublicationRecordResponse(
        id=record.id,
        tenant_id=record.tenant_id,
        artifact_type=record.artifact_type,
        artifact_id=record.artifact_id,
        version_id=record.version_id,
        action=record.action,  # type: ignore[arg-type]
        actor_id=record.actor_id,
        occurred_at=record.occurred_at.isoformat(),
        change_summary=record.change_summary,
    )


@router.get(
    "",
    response_model=PublicationHistoryResponse,
    summary="List publication history (FM-021)",
)
async def list_publications(
    artifact_type: Annotated[str | None, Query(max_length=64)] = None,
    artifact_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminGovernanceHttpDependencyProvider = Depends(
        get_admin_governance_dependencies
    ),
) -> PublicationHistoryResponse:
    """List publication history, optionally scoped to one artifact.

    The result is newest-first. When ``artifact_id`` is provided, the
    query is scoped to that artifact (the per-version tab in the admin
    UI). When only ``artifact_type`` is provided, the result is the
    tenant-wide timeline filtered by type (e.g. ``CaseTypeVersion``).
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="configuration"),
        role_repository=repositories.role,
    )

    records = await governance.list_publications(
        governance.ListPublicationsQuery(
            tenant_id=user.tenant_id,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            limit=limit,
            offset=offset,
        ),
        repo=repositories.publication,
    )
    return PublicationHistoryResponse(items=[_to_response(r) for r in records])


@router.post(
    "",
    response_model=RecordPublicationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Append a publication row (FM-021)",
)
async def record_publication(
    body: RecordPublicationRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminGovernanceHttpDependencyProvider = Depends(
        get_admin_governance_dependencies
    ),
) -> RecordPublicationResponse:
    """Append a publication row for the authenticated administrator.

    The action vocabulary is ``draft | validate | publish | retire``.
    For ``publish``, the response carries ``approval_required=True``
    when the tenant's policy requires a second administrator (P10-US8);
    the caller MUST then drive the approval flow from a different
    identity before the publish is considered effective.
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="configuration"),
        role_repository=repositories.role,
    )

    result = await governance.record_publication(
        governance.RecordPublicationCommand(
            tenant_id=user.tenant_id,
            artifact_type=body.artifact_type,
            artifact_id=body.artifact_id,
            version_id=body.version_id,
            action=body.action,
            actor_id=user.user_id,
            change_summary=body.change_summary,
            correlation_id=correlation.correlation_id,
        ),
        repo=repositories.publication,
        policy=repositories.policy,
        clock=clock,
    )
    await db.commit()
    return RecordPublicationResponse(
        publication_id=result.publication_id,
        action=body.action,
        approval_required=result.approval_required,
    )


@router.post(
    "/approve",
    response_model=ApprovePublicationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a second administrator's approval (P10-US8)",
)
async def approve_publication(
    body: ApprovePublicationRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminGovernanceHttpDependencyProvider = Depends(
        get_admin_governance_dependencies
    ),
) -> ApprovePublicationResponse:
    """Record a second administrator's approval of a publish (P10-US8).

    The approver's ``actor_id`` is supplied in the body so the test
    surface can drive the approval from a different identity than the
    publish request. In production the field is set by the
    authenticated user (a separate admin session). The use case
    enforces separation-of-duties: a same-actor attempt raises
    ``Forbidden`` (HTTP 403).
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="configuration"),
        role_repository=repositories.role,
    )

    record = await governance.approve_publication(
        governance.ApprovePublicationCommand(
            tenant_id=user.tenant_id,
            artifact_type=body.artifact_type,
            artifact_id=body.artifact_id,
            version_id=body.version_id,
            approving_actor_id=body.approving_actor_id,
            change_summary=body.change_summary,
            correlation_id=correlation.correlation_id,
        ),
        repo=repositories.publication,
        clock=clock,
    )
    await db.commit()
    return ApprovePublicationResponse(
        publication_id=record.id,
        action="validate",
    )


__all__ = ["router"]
