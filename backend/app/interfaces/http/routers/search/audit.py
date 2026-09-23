"""Saved-search audit report route: ``GET /search_audit`` (P06-US5 / FM-032).

Security-staff report answering "who searched for what and saw how many
results": one row per saved-search save + run event, with the query
shape and the result count exposed verbatim. Gated on ``audit.read`` by
the application use case; ad-hoc rows (``saved_search_id=None``) are
written by
:func:`app.interfaces.http.routers.search.common._audit_adhoc_search`
and are visible here alongside the saved-search events.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.search import saved_searches as saved_searches_uc
from app.application.search.http_dependencies import SearchHttpDependencyProvider
from app.deps import UserContext, get_current_user, get_db
from app.interfaces.http.dependencies import get_search_dependencies
from app.interfaces.http.schemas.search import (
    SearchAuditEntryResponse,
    SearchAuditList,
)

from .common import router

# ---------------------------------------------------------------------------
# GET /api/v1/search_audit (P06-US5 / FM-032)
# ---------------------------------------------------------------------------


@router.get(
    "/search_audit",
    response_model=SearchAuditList,
    summary="Saved-search audit report (security staff)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks audit.read"},
    },
)
async def get_search_audit(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: SearchHttpDependencyProvider = Depends(get_search_dependencies),
    actor_id: Annotated[
        UUID | None,
        Query(description="Filter by actor id (UUID)"),
    ] = None,
    saved_search_id: Annotated[
        UUID | None,
        Query(description="Filter by saved-search id (UUID)"),
    ] = None,
    since: Annotated[
        datetime | None,
        Query(description="Inclusive lower bound on occurred_at (ISO-8601)"),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(description="Inclusive upper bound on occurred_at (ISO-8601)"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SearchAuditList:
    """Security-staff search audit (P06-US5 / FM-032).

    Returns one row per saved-search save + run event. The query
    shape and the result count are exposed verbatim so the report can
    answer "who searched for what and saw how many results".
    """
    audit_repo = dependencies.for_session(db).saved_search_audit
    page = await saved_searches_uc.view_search_audit(
        saved_searches_uc.ViewSearchAuditQuery(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            actor_filter=actor_id,
            saved_search_id=saved_search_id,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        ),
        audit=audit_repo,
    )
    return SearchAuditList(
        items=[
            SearchAuditEntryResponse(
                id=item.id,
                occurred_at=item.occurred_at,
                actor_id=item.actor_id,
                saved_search_id=item.saved_search_id,
                query_shape=item.query_shape,
                result_count=item.result_count,
            )
            for item in page.items
        ],
        limit=page.limit,
    )
