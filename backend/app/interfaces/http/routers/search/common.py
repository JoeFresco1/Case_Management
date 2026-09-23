"""Shared ``router`` + helpers + execution core for the search HTTP surface.

# Why this module (R2 structural split)

Formerly a single ``search.py`` module of 925 lines — past the R2 600-line
soft limit. Split by route-group responsibility into :mod:`adhoc`,
:mod:`semantic`, :mod:`saved`, :mod:`audit`, with the pieces every route
group needs living here:

- :data:`router` — the one ``APIRouter(tags=["search"])`` the route
  modules register onto, so ``app.main`` still imports a single ``router``
  from the package (``from ...search import router`` — consumer contract
  unchanged).
- :func:`_resolve_field_keys` — the single point where the P05 schema
  contract (searchable / sensitive field keys) crosses into P06 search.
- :func:`_hit_to_wire` / :func:`_validate_filter_request` — wire ↔
  application projections and the structured 400 for unsupported
  operators.
- :func:`_adhoc_audit_shape` / :func:`_audit_adhoc_search` — the PHI-free
  ``search_audit`` row appended on every ad-hoc execution
  (``saved_search_id=None``), committed best-effort so an observability
  outage never becomes a retrieval outage on a read endpoint.
- :func:`_run_search` — shared execution for the three ad-hoc search
  endpoints: resolve field keys → run the use case → audit → translate.

Registration order across route modules is irrelevant: no two search
paths overlap at the same segment depth and method, so Starlette's
first-match rule resolves identically regardless of import order.

# INTEGRATION_SEAM: search engine

The router resolves the searchable / sensitive field-key sets by reading
the tenant's published CaseTypeVersions through the same repositories
the case-creation flow already uses. The two sets are the P05 contract
(the schema is the source of truth for what is queryable / sensitive)
and are passed into the use case as plain tuples so the application
layer stays pure.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.search import case_search
from app.application.search.http_dependencies import (
    SearchFieldKeyResolver,
    SearchHttpRepositories,
)
from app.deps import Clock, UserContext
from app.domain.errors import ValidationFailed
from app.domain.search import QueryFilter, SearchQuery, query_shape_for_audit
from app.interfaces.http.schemas.search import (
    CaseSearchHit,
    CaseSearchResponse,
    SearchFilterRequest,
)

router = APIRouter(tags=["search"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_field_keys(
    *,
    field_keys: SearchFieldKeyResolver,
    tenant_id: UUID,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(searchable_keys, sensitive_keys)`` for the tenant.

    Delegates to the ``SearchFieldKeyResolver`` port supplied by the
    composition root, which unions the field-key sets across the
    tenant's published ``CaseTypeVersion`` schemas. The application use
    case consumes the two tuples as sealed inputs; this helper is the
    single point where the P05 schema contract crosses into P06
    search.
    """
    return await field_keys.field_keys(tenant_id)


def _hit_to_wire(hit) -> CaseSearchHit:
    """Translate an application ``SearchHit`` into the wire model.

    The snippet's values are passed through unchanged; per-field
    redaction has already happened in the SQL adapter (P11-US6 /
    P05-US3). The wire shape is a plain dict so the OpenAPI schema is
    honest about the mix of scalars and ``RedactedField`` markers.
    """
    return CaseSearchHit(
        id=hit.case_id,
        reference=hit.reference,
        priority=hit.priority,
        status=hit.status,
        title=hit.title,
        data_snippet=hit.data_snippet,
    )


def _validate_filter_request(
    filters: list[SearchFilterRequest],
) -> tuple[tuple[str, str, object], ...]:
    """Translate the wire ``SearchFilterRequest`` list into the app shape.

    The application use case owns the operator vocabulary; the router
    only re-uses it to surface a structured 400 when a caller asks for
    an unsupported operator. Anything else is forwarded untouched.
    """
    out: list[tuple[str, str, object]] = []
    for f in filters:
        if f.op not in case_search.SUPPORTED_OPS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "validation_failed",
                    "message": (
                        f"unsupported filter operator {f.op!r}; "
                        f"supported: {sorted(case_search.SUPPORTED_OPS)}"
                    ),
                },
            )
        out.append((f.field, f.op, f.value))
    return tuple(out)


# ---------------------------------------------------------------------------
# Shared execution
# ---------------------------------------------------------------------------


def _adhoc_audit_shape(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    text_query: str | None,
    filters: list[SearchFilterRequest],
    searchable_keys: tuple[str, ...],
    sensitive_keys: tuple[str, ...],
) -> dict[str, Any]:
    """Build the canonical PHI-free query shape for an ad-hoc audit row.

    Reuses the domain projection (``query_shape_for_audit``) so filter
    values targeting a sensitive key are stored as ``ref`` markers,
    never as content. The row therefore answers "who searched for what
    and saw how many results" without persisting payload PHI.

    The inputs are pre-validated (operator vocabulary + searchable
    gating already passed), so construction cannot fail in practice;
    a defensive fallback drops filter values entirely rather than
    risking a raw value in the audit row.
    """
    try:
        domain_query = SearchQuery(
            tenant_id=tenant_id,
            actor_id=actor_id,
            text_query=text_query,
            filters=tuple(QueryFilter(field=f.field, op=f.op, value=f.value) for f in filters),
            searchable_field_keys=searchable_keys,
            sensitive_field_keys=sensitive_keys,
        )
    except ValidationFailed:
        # why fallback without values: the search itself was already
        # rejected upstream; the audit row must still be safe to write,
        # so field identity is kept but every value is omitted.
        return {
            "text_query": text_query,
            "filters": [{"field": f.field, "op": f.op} for f in filters],
            "sort": [],
        }
    return dict(query_shape_for_audit(domain_query))


async def _audit_adhoc_search(
    *,
    db: AsyncSession,
    repositories: SearchHttpRepositories,
    tenant_id: UUID,
    actor_id: UUID,
    query_shape: dict[str, Any],
    result_count: int,
    clock: Clock,
) -> None:
    """Append one ad-hoc ``search_audit`` row (``saved_search_id=None``).

    The row is committed in the request transaction next to the search
    read. A failing audit write rolls back only the audit row — the
    search page is still returned, because an observability outage must
    not become a retrieval outage on a read endpoint (the reusable
    saved-search path stays strict: save + run commit audit atomically
    with the domain row).
    """
    try:
        await repositories.saved_search_audit.append(
            tenant_id=tenant_id,
            actor_id=actor_id,
            saved_search_id=None,
            query_shape=query_shape,
            result_count=result_count,
            occurred_at=clock.now(),
        )
        await db.commit()
    except Exception:
        # why swallow: the audit row is observability, not the
        # retrieval contract; a full rollback here would turn an audit
        # outage into a search outage for every worker.
        await db.rollback()


async def _run_search(
    *,
    user: UserContext,
    db: AsyncSession,
    repositories: SearchHttpRepositories,
    query: str | None,
    filters: list[SearchFilterRequest],
    limit: int,
    offset: int,
    clock: Clock,
) -> CaseSearchResponse:
    """Resolve field keys + run the use case + audit + translate."""
    searchable_keys, sensitive_keys = await _resolve_field_keys(
        field_keys=repositories.field_keys, tenant_id=user.tenant_id
    )
    port_filters = _validate_filter_request(filters)
    provider = repositories.search

    try:
        result = await case_search.handle(
            case_search.CaseSearchQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                text_query=query,
                filters=port_filters,
                limit=limit,
                offset=offset,
                searchable_field_keys=searchable_keys,
                sensitive_field_keys=sensitive_keys,
            ),
            search_provider=provider,
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
