"""Semantic (AI-ranked) case-search route: ``POST /search/semantic`` (P06-US7 / FM-034).

Accepts the same JSON body as ``POST /api/v1/search``. The use case
authorizes the actor first, runs the structured provider to get the
authorized candidate set, then asks the ``SearchRanker`` to reorder it.
The ranker can never widen the candidate set; deterministic
authorization remains authoritative (Constitution XIV — bounded AI
assistance).

# INTEGRATION_SEAM: search ranker

The ``SearchRanker`` port is reached through the session's dependency
bundle (``repositories.ranker``). The local adapter is the no-op ranker
that preserves structured order (FM-050); an Azure-side ranker plugs in
behind the same port without touching this router.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.search import semantic_search
from app.application.search.http_dependencies import (
    SearchHttpDependencyProvider,
    SearchHttpRepositories,
)
from app.deps import (
    Clock,
    CorrelationContext,
    SystemClock,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.errors import ValidationFailed
from app.interfaces.http.dependencies import get_search_dependencies
from app.interfaces.http.schemas.search import (
    CaseSearchRequest,
    CaseSearchResponse,
    SearchFilterRequest,
)

from .common import (
    _adhoc_audit_shape,
    _audit_adhoc_search,
    _hit_to_wire,
    _resolve_field_keys,
    _validate_filter_request,
    router,
)

# ---------------------------------------------------------------------------
# POST /api/v1/search/semantic (P06-US7 / FM-034)
# ---------------------------------------------------------------------------


@router.post(
    "/search/semantic",
    response_model=CaseSearchResponse,
    summary="Semantic (AI-ranked) case search",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Unknown field or non-searchable field in filter"
        },
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
    },
)
async def post_semantic_search(
    body: CaseSearchRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: SearchHttpDependencyProvider = Depends(get_search_dependencies),
    correlation: CorrelationContext = Depends(get_correlation_id),
    clock: Annotated[Clock, Depends(get_clock)] = SystemClock(),
) -> CaseSearchResponse:
    """POST-flavored semantic (AI-ranked) case search (P06-US7 / FM-034).

    Accepts the same JSON body as ``POST /api/v1/search``. The use case
    authorizes the actor first, runs the structured provider to get the
    authorized candidate set, then asks the ``SearchRanker`` to reorder
    it. The ranker can never widen the candidate set; deterministic
    authorization remains authoritative.

    Args:
        body: Free-text query plus declarative ``{field, op, value}``
            filters with ``limit`` / ``offset`` pagination.
        user: Authenticated actor (tenant + roles) from the identity seam.
        db: Request-scoped transaction session.
        dependencies: Search port bundle for the session.
        correlation: Request correlation id, carried into the ranking
            hint so the AI recommendation log line joins the timeline.

    Returns:
        The AI-ordered page in the same wire shape as the structured
        search (``CaseSearchResponse``).

    Failure mode:
        Unsupported operator or non-searchable field raises 400;
        an actor without ``case.read`` raises 403 via the domain
        error handler.
    """
    return await _run_semantic_search(
        user=user,
        db=db,
        repositories=dependencies.for_session(db),
        query=body.query,
        filters=body.filters,
        limit=body.limit,
        offset=body.offset,
        correlation_id=correlation.correlation_id,
        clock=clock,
    )


async def _run_semantic_search(
    *,
    user: UserContext,
    db: AsyncSession,
    repositories: SearchHttpRepositories,
    query: str | None,
    filters: list[SearchFilterRequest],
    limit: int,
    offset: int,
    correlation_id: str,
    clock: Clock,
) -> CaseSearchResponse:
    """Resolve field keys + run the semantic use case + translate the response.

    Mirrors :func:`app.interfaces.http.routers.search.common._run_search`:
    the P05 schema contract is resolved for the actor's tenant, the wire
    filters are validated to the application shape, and the hits are
    projected with the same :func:`~app.interfaces.http.routers.search.common._hit_to_wire`
    shape so clients see one search envelope.

    Args:
        user: Authenticated actor (tenant + roles) from the identity seam.
        repositories: Search port bundle for the request transaction,
            including the ``SearchRanker`` AI port.
        query: Free-text portion matched across ``title`` and declared
            searchable ``data`` fields.
        filters: Wire ``{field, op, value}`` filters.
        limit: Page size; clamped to ``[1, 200]`` by the use case.
        offset: Pagination offset; non-negative.
        correlation_id: Carried into the ranking hint and the
            recommendation log line.

    Returns:
        The AI-ordered page as ``CaseSearchResponse``.

    Failure mode:
        ``ValidationFailed`` (unknown operator / non-searchable field)
        maps to 400; ``Forbidden`` propagates to the domain error
        handler which maps it to 403.
    """
    searchable_keys, sensitive_keys = await _resolve_field_keys(
        field_keys=repositories.field_keys, tenant_id=user.tenant_id
    )
    port_filters = _validate_filter_request(filters)

    try:
        result = await semantic_search.handle(
            semantic_search.SemanticSearchQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                correlation_id=correlation_id,
                text_query=query,
                filters=port_filters,
                limit=limit,
                offset=offset,
                searchable_field_keys=searchable_keys,
                sensitive_field_keys=sensitive_keys,
                ranking=True,
            ),
            search_provider=repositories.search,
            ranker=repositories.ranker,
        )
    except ValidationFailed as exc:
        # Field-not-searchable -> 400 per acceptance criteria.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "validation_failed",
                "message": str(exc),
                "field": exc.field,
            },
        ) from exc

    await _audit_adhoc_search(
        db=db,
        repositories=repositories,
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        query_shape=_adhoc_audit_shape(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            text_query=query,
            filters=filters,
            searchable_keys=searchable_keys,
            sensitive_keys=sensitive_keys,
        ),
        result_count=result.total_considered,
        clock=clock,
    )

    return CaseSearchResponse(
        items=[_hit_to_wire(h) for h in result.items],
        next_cursor=result.next_cursor,
        limit=result.limit,
        total_considered=result.total_considered,
    )
