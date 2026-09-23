"""Saved-search routes: save / list / delete / run (P06-US3, US5, US6 / FM-032).

Reusable case-search queries, owner-scoped (P11 / FM-002 — cross-actor
reads are not exposed at the wire). Save + run commit the domain row and
its ``search_audit`` row atomically in the request transaction; delete
and run map ``NotFound`` to 404 so a cross-tenant id is never confirmed.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.search import saved_searches as saved_searches_uc
from app.application.search.http_dependencies import SearchHttpDependencyProvider
from app.deps import Clock, SystemClock, UserContext, get_clock, get_current_user, get_db
from app.domain.errors import Conflict, NotFound, ValidationFailed
from app.domain.saved_search import SortSpec
from app.interfaces.http.dependencies import get_search_dependencies
from app.interfaces.http.schemas.search import (
    CaseSearchResponse,
    SavedSearchList,
    SavedSearchRequest,
    SavedSearchResponse,
    SearchFilterRequest,
    SortEntry,
)

from .common import _hit_to_wire, _resolve_field_keys, router


def _saved_to_wire(saved) -> SavedSearchResponse:
    """Project a domain ``SavedSearch`` onto the wire schema.

    The shape mirrors :meth:`SavedSearch.to_wire` so a future
    auto-generated client stays consistent with the dataclass.
    """
    return SavedSearchResponse(
        id=saved.id,
        name=saved.name,
        query=saved.text_query,
        filters=[SearchFilterRequest(field=f[0], op=f[1], value=f[2]) for f in saved.filters],
        sort=[SortEntry(field=s.field, direction=s.direction) for s in saved.sort],
        query_shape=dict(saved.query_shape),
        created_at=(saved.created_at.isoformat() if saved.created_at is not None else None),
    )


@router.post(
    "/search/saved",
    response_model=SavedSearchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Save a case-search query for reuse",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Validation failed (unsupported operator, empty in-list, sort field)"
        },
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
        status.HTTP_409_CONFLICT: {
            "description": "A saved search with this name already exists for the owner"
        },
    },
)
async def post_saved_search(
    body: SavedSearchRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: SearchHttpDependencyProvider = Depends(get_search_dependencies),
    clock: Annotated[Clock, Depends(get_clock)] = SystemClock(),
) -> SavedSearchResponse:
    """Persist a named, reusable case-search query (P06-US3 / FM-032).

    The use case validates the operator vocabulary and the sort field
    set; the SQL adapter carries the unique constraint on
    ``(tenant_id, owner_user_id, name)`` so a duplicate name surfaces
    as ``Conflict`` (mapped to 409).
    """
    repositories = dependencies.for_session(db)
    repo = repositories.saved_search
    audit_repo = repositories.saved_search_audit
    filters = tuple((f.field, f.op, f.value) for f in body.filters)
    sort = tuple(SortSpec(field=s.field, direction=s.direction) for s in body.sort)
    try:
        saved = await saved_searches_uc.save_search(
            saved_searches_uc.SaveSearchCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                name=body.name,
                text_query=body.query,
                filters=filters,
                sort=sort,
            ),
            repository=repo,
            audit=audit_repo,
            clock=clock,
        )
        await db.commit()
    except ValidationFailed as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "validation_failed",
                "message": str(exc),
                "field": exc.field,
            },
        ) from exc
    except Conflict as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "conflict", "message": str(exc)},
        ) from exc
    return _saved_to_wire(saved)


@router.get(
    "/search/saved",
    response_model=SavedSearchList,
    summary="List the actor's saved case-search queries",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
    },
)
async def list_saved_searches(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: SearchHttpDependencyProvider = Depends(get_search_dependencies),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SavedSearchList:
    """Return the actor's saved searches, newest first.

    Owner-scoped: only the actor's own rows are visible. Cross-actor
    reads are not exposed at the wire (P11 / FM-002).
    """
    repo = dependencies.for_session(db).saved_search
    rows = await saved_searches_uc.list_saved_searches(
        saved_searches_uc.ListSavedSearchesQuery(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            limit=limit,
            offset=offset,
        ),
        repository=repo,
    )
    return SavedSearchList(items=[_saved_to_wire(s) for s in rows], limit=limit)


@router.delete(
    "/search/saved/{saved_search_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a saved case-search query (owner only)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
        status.HTTP_404_NOT_FOUND: {
            "description": "Saved search missing or not owned by the actor"
        },
    },
)
async def delete_saved_search(
    saved_search_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: SearchHttpDependencyProvider = Depends(get_search_dependencies),
) -> Response:
    """Delete the actor's saved-search row (P06-US3 / FM-032).

    A missing id and a cross-tenant id both surface as 404; a row
    owned by a different actor is also 404 (the use case never
    confirms the row exists for someone else).
    """
    repo = dependencies.for_session(db).saved_search
    try:
        await saved_searches_uc.delete_saved_search(
            saved_searches_uc.DeleteSavedSearchCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                saved_search_id=saved_search_id,
            ),
            repository=repo,
        )
        await db.commit()
    except NotFound as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": str(exc)},
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/search/saved/{saved_search_id}/run",
    response_model=CaseSearchResponse,
    summary="Re-execute a saved case-search query",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
        status.HTTP_404_NOT_FOUND: {
            "description": "Saved search missing or not owned by the actor"
        },
    },
)
async def run_saved_search(
    saved_search_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: SearchHttpDependencyProvider = Depends(get_search_dependencies),
    clock: Annotated[Clock, Depends(get_clock)] = SystemClock(),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CaseSearchResponse:
    """Re-execute a previously saved search (P06-US3 / FM-032).

    The execution path goes through the live ``SqlSearchProvider`` so
    the result set matches what the actor would see with the same
    query shape typed by hand. A ``SearchAudit`` row carrying the
    canonical query shape, timestamp, and result count is appended in
    the same transaction (P06-US6).
    """
    repositories = dependencies.for_session(db)
    repo = repositories.saved_search
    audit_repo = repositories.saved_search_audit
    provider = repositories.search
    searchable_keys, sensitive_keys = await _resolve_field_keys(
        field_keys=repositories.field_keys, tenant_id=user.tenant_id
    )
    try:
        result = await saved_searches_uc.run_saved_search(
            saved_searches_uc.RunSavedSearchCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                saved_search_id=saved_search_id,
                searchable_field_keys=searchable_keys,
                sensitive_field_keys=sensitive_keys,
                limit=limit,
                offset=offset,
            ),
            repository=repo,
            audit=audit_repo,
            search_provider=provider,
            clock=clock,
        )
        await db.commit()
    except NotFound as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": str(exc)},
        ) from exc
    except ValidationFailed as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "validation_failed",
                "message": str(exc),
                "field": exc.field,
            },
        ) from exc
    return CaseSearchResponse(
        items=[_hit_to_wire(h) for h in result.items],
        next_cursor=result.next_cursor,
        limit=result.limit,
        total_considered=result.total_considered,
    )
