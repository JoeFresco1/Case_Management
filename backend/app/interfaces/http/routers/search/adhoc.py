"""Ad-hoc case-search routes (P06-US1 / FM-031).

``GET /cases/search``, ``GET /search/cases``, and ``POST /search`` —

Three spellings of the same declarative search so browser navigation,
plan-contract clients (``specs/002-leftovers/plan.md §4``), and richer
JSON clients (TanStack Query, scripted admin tools) all converge on one
use case, one authorization gate, and one audit row. Every execution
delegates to :func:`app.interfaces.http.routers.search.common._run_search`,
which appends the PHI-free ``search_audit`` row and maps a
non-searchable field to 400.

The router only translates between the wire shape and the application
shape; no business logic lives here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.search.http_dependencies import SearchHttpDependencyProvider
from app.deps import Clock, SystemClock, UserContext, get_clock, get_current_user, get_db
from app.interfaces.http.dependencies import get_search_dependencies
from app.interfaces.http.pagination import MAX_LIMIT
from app.interfaces.http.schemas.search import (
    CaseSearchRequest,
    CaseSearchResponse,
    SearchFilterRequest,
)

from .common import _run_search, router

# ---------------------------------------------------------------------------
# GET /api/v1/cases/search (P06-US1 / FM-031)
# ---------------------------------------------------------------------------


@router.get(
    "/cases/search",
    response_model=CaseSearchResponse,
    summary="Search cases for the current tenant (declarative)",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Unknown field or non-searchable field in filter"
        },
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
    },
)
async def get_search(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: SearchHttpDependencyProvider = Depends(get_search_dependencies),
    clock: Annotated[Clock, Depends(get_clock)] = SystemClock(),
    query: Annotated[str | None, Query(max_length=500)] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    priority: Annotated[str | None, Query()] = None,
    field_key: Annotated[str | None, Query(alias="data")] = None,
    field_value: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CaseSearchResponse:
    """GET-flavored case search.

    Browser-friendly parameters: ``query``, ``status``, ``priority``,
    and an optional pair ``data=<key>``, ``<implicit>=<value>`` for a
    single ``data.<key>`` filter. Richer clients should use the POST
    variant.
    """
    filters: list[SearchFilterRequest] = []
    if status_filter is not None:
        filters.append(SearchFilterRequest(field="status", op="eq", value=status_filter))
    if priority is not None:
        filters.append(SearchFilterRequest(field="priority", op="eq", value=priority))
    if field_key is not None and field_value is not None:
        filters.append(SearchFilterRequest(field=f"data.{field_key}", op="eq", value=field_value))
    return await _run_search(
        user=user,
        db=db,
        repositories=dependencies.for_session(db),
        query=query,
        filters=filters,
        limit=limit,
        offset=offset,
        clock=clock,
    )


@router.get(
    "/search/cases",
    response_model=CaseSearchResponse,
    summary="Search cases for the current tenant (plan contract path)",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Unknown field or non-searchable field in filter"
        },
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
    },
)
async def get_search_cases(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: SearchHttpDependencyProvider = Depends(get_search_dependencies),
    clock: Annotated[Clock, Depends(get_clock)] = SystemClock(),
    query: Annotated[str | None, Query(max_length=500)] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    priority: Annotated[str | None, Query()] = None,
    field_key: Annotated[str | None, Query(alias="data")] = None,
    field_value: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CaseSearchResponse:
    """Alias of ``GET /cases/search`` on the plan contract path.

    ``specs/002-leftovers/plan.md §4`` names this route
    ``GET /search/cases``; the implementation keeps both spellings so
    linkable browser URLs and plan-shaped clients converge on one use
    case, one authorization gate, and one audit row.
    """
    filters: list[SearchFilterRequest] = []
    if status_filter is not None:
        filters.append(SearchFilterRequest(field="status", op="eq", value=status_filter))
    if priority is not None:
        filters.append(SearchFilterRequest(field="priority", op="eq", value=priority))
    if field_key is not None and field_value is not None:
        filters.append(SearchFilterRequest(field=f"data.{field_key}", op="eq", value=field_value))
    return await _run_search(
        user=user,
        db=db,
        repositories=dependencies.for_session(db),
        query=query,
        filters=filters,
        limit=limit,
        offset=offset,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/search (P06-US1 / FM-031)
# ---------------------------------------------------------------------------


@router.post(
    "/search",
    response_model=CaseSearchResponse,
    summary="Search cases (POST body)",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Unknown field or non-searchable field in filter"
        },
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
    },
)
async def post_search(
    body: CaseSearchRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: SearchHttpDependencyProvider = Depends(get_search_dependencies),
    clock: Annotated[Clock, Depends(get_clock)] = SystemClock(),
) -> CaseSearchResponse:
    """POST-flavored case search.

    Accepts a JSON body with the same shape as the GET variant plus a
    list of ``{field, op, value}`` filters. ``limit`` / ``offset`` are
    clamped at the Pydantic layer; the use case clamps again at the
    application boundary.
    """
    return await _run_search(
        user=user,
        db=db,
        repositories=dependencies.for_session(db),
        query=body.query,
        filters=body.filters,
        limit=body.limit,
        offset=body.offset,
        clock=clock,
    )
