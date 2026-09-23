"""Access-decision report route — ``GET /api/v1/audit/access-decisions`` (P09-US6; FM-045).

Returns a filtered page of access decisions so a security
administrator can answer "who tried to access which resource and why".
Both allowed and denied decisions are visible; the ``result`` filter
narrows to one outcome when needed (default: both).

Authorization: ``AUDIT_READ`` is required and is evaluated up-front via
``authorize`` so a forbidden actor never sees report rows. The store's
``list_access_decisions`` re-applies the tenant filter so cross-tenant
ids surface as ``not_found`` through row absence (Constitution V).

Registers onto the package :mod:`common` audit ``router``.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import access_audit as access_audit_uc
from app.application.audit.http_dependencies import AuditHttpDependencyProvider
from app.application.authorization import Action, Resource, authorize
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.errors import ValidationFailed
from app.interfaces.http.dependencies import get_audit_dependencies
from app.interfaces.http.schemas.audit import AccessDecisionPage, AccessDecisionResponse

from .common import router


@router.get(
    "/audit/access-decisions",
    response_model=AccessDecisionPage,
    summary="Security access-decision report (allowed + denied)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks audit.read"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Malformed filter or cursor"},
    },
)
async def get_access_decisions(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AuditHttpDependencyProvider = Depends(get_audit_dependencies),
    correlation: CorrelationContext = Depends(get_correlation_id),
    clock: Clock = Depends(get_clock),
    actor_id: Annotated[
        UUID | None,
        Query(description="Filter by actor id (UUID)"),
    ] = None,
    subject_type: Annotated[
        str | None,
        Query(description="Filter by resource kind (e.g. 'case')"),
    ] = None,
    action: Annotated[
        str | None,
        Query(description="Filter by evaluated action (e.g. 'case.read_sensitive')"),
    ] = None,
    result: Annotated[
        str | None,
        Query(description="Filter by decision result ('allowed' or 'denied')"),
    ] = None,
    since: Annotated[
        datetime | None,
        Query(description="Inclusive lower bound on occurred_at (ISO-8601)"),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(description="Inclusive upper bound on occurred_at (ISO-8601)"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = access_audit_uc.DEFAULT_PAGE_LIMIT,
    cursor: Annotated[
        str | None,
        Query(description="Opaque pagination cursor from a prior page"),
    ] = None,
) -> AccessDecisionPage:
    """Return a filtered page of access decisions (P09-US6 / FM-045).

    A security administrator uses this endpoint to answer "who tried
    to access which resource and why". Both allowed and denied
    decisions are visible; the ``result`` filter narrows to one
    outcome when needed (default: both).

    Args:
        user: Authenticated caller (identity + tenant + roles).
        db: Request-scoped async DB session.
        dependencies: Audit repository bundle for this session.
        correlation: Request correlation id for the access recorder.
        clock: Wall clock used by the access recorder.
        actor_id: Optional filter on the deciding actor id.
        subject_type: Optional filter on the resource kind.
        action: Optional filter on the evaluated action key.
        result: Optional filter on ``allowed`` / ``denied``.
        since: Inclusive lower bound on ``occurred_at``.
        until: Inclusive upper bound on ``occurred_at``.
        limit: Page size (1..200; default from the use case).
        cursor: Opaque pagination cursor from a prior page.

    Returns:
        One page of access-decision rows, newest first.

    Raises:
        HTTPException: 403 when ``AUDIT_READ`` is missing, 422 on a
            malformed filter or cursor.
    """
    repositories = dependencies.for_session(db)
    # Validate the cursor early so a malformed token surfaces as 422
    # instead of 500.
    if cursor:
        try:
            repositories.cursor_decoder(cursor)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "validation_failed",
                    "message": f"malformed cursor: {exc}",
                    "field": "cursor",
                },
            ) from exc

    # Authorize the report read. AUDIT_READ scopes to the actor's
    # tenant; the store's ``list_access_decisions`` re-applies the
    # tenant filter so cross-tenant ids surface as ``not_found``
    # through the row absence (Constitution V).
    await authorize(
        Action.AUDIT_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="audit_report"),
        access_recorder=access_audit_uc.build_recorder(
            audit=repositories.audit,
            clock=clock,
        ),
        correlation_id=correlation.correlation_id,
    )

    try:
        page = await access_audit_uc.view_report(
            access_audit_uc.AccessDecisionQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                actor_filter=actor_id,
                subject_type=subject_type,
                action=action,
                result=result,
                since=since,
                until=until,
                limit=limit,
                cursor=cursor,
            ),
            audit=repositories.audit,
            cursor_encoder=repositories.cursor_encoder,
        )
    except ValidationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "validation_failed",
                "message": str(exc),
                "field": exc.field,
            },
        ) from exc

    return AccessDecisionPage(
        items=[AccessDecisionResponse.model_validate(asdict(e)) for e in page.items],
        next_cursor=page.next_cursor,
        limit=page.limit,
    )
