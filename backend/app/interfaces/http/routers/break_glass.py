"""HTTP router for the break-glass surface (P11-US10 / FM-035 / P06-US9).

Endpoints:

- ``POST   /api/v1/security/break-glass`` — issue a time-bounded,
  reason-bearing cross-tenant override (requires process_admin).
- ``DELETE /api/v1/security/break-glass/{grant_id}`` — revoke a grant.
- ``GET    /api/v1/security/break-glass/cases/{case_id}`` — read a
  case in another tenant under an active grant.
- ``POST   /api/v1/security/break-glass/search`` — search cases in
  another tenant under an active grant.

The router is the boundary where a process administrator grants and
revokes time-bounded, reason-bearing overrides, and where auditors
exercise them: cross-tenant case read and cross-tenant case search.
Grants are durable rows (``break_glass_grants``) sharing the
request transaction, so issuance, revocation, and use are atomic
with their audit rows. Every action under a grant is flagged via
``record_break_glass_action`` and surfaced on the wire through the
``X-Break-Glass-Grant`` response header.

Routes are included into the parent security router (which owns the
``/security`` prefix and ``security`` tag) via
``router.include_router(break_glass.router)`` in ``security.py``;
this child router must therefore declare no prefix and no tags.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases import view_case as view_case_uc
from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.application.cases.summary import build_summary, summary_to_dict
from app.application.search import case_search
from app.application.search.http_dependencies import SearchHttpDependencyProvider
from app.application.security import break_glass
from app.application.security.http_dependencies import SecurityHttpDependencyProvider
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.break_glass import BreakGlassGrant, BreakGlassScope
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.ports import SearchFilter
from app.interfaces.http.dependencies import (
    get_case_dependencies,
    get_search_dependencies,
    get_security_dependencies,
)
from app.interfaces.http.schemas.cases import CaseSummary as CaseSummarySchema
from app.interfaces.http.schemas.search import (
    CaseSearchHit,
    CaseSearchResponse,
    SearchFilterRequest,
)

# No prefix and no tags: the parent security router supplies both so
# paths stay ``/api/v1/security/...`` and OpenAPI tags stay unchanged.
router = APIRouter()


# ---------------------------------------------------------------------------
# Wire shapes for the break-glass surface
# ---------------------------------------------------------------------------


class GrantBreakGlassRequest(BaseModel):
    target_user_id: UUID
    scope: BreakGlassScope
    reason: str = Field(min_length=1, max_length=1024)
    duration_minutes: int = Field(gt=0, le=24 * 60)


class BreakGlassGrantResponse(BaseModel):
    grant_id: UUID
    tenant_id: UUID
    target_user_id: UUID
    scope: BreakGlassScope
    reason: str
    granted_by: UUID
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_by: UUID | None = None


def _grant_to_response(grant: BreakGlassGrant) -> BreakGlassGrantResponse:
    return BreakGlassGrantResponse(
        grant_id=grant.id,
        tenant_id=grant.tenant_id,
        target_user_id=grant.user_id,
        scope=grant.scope,
        reason=grant.reason,
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
        revoked_by=grant.revoked_by,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/break-glass",
    response_model=BreakGlassGrantResponse,
    status_code=status.HTTP_201_CREATED,
    summary=(
        "Issue a time-bounded, reason-bearing break-glass override "
        "(P11-US10 / FM-035). Requires process_admin."
    ),
)
async def create_break_glass_grant(
    body: GrantBreakGlassRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: SecurityHttpDependencyProvider = Depends(get_security_dependencies),
) -> BreakGlassGrantResponse:
    """Mint a new break-glass grant for ``body.target_user_id``.

    The endpoint authorizes against ``ADMIN_TENANT_CONFIG`` so only
    a process administrator can mint overrides; auditors do not
    grant their own. Issue the grant in the **data-owning** tenant
    (the tenant whose cases the grantee may access) — the read-side
    enforcement looks the grant up there. The use case writes a
    ``BreakGlassGranted`` audit row carrying the reason, the scope,
    and the expiry so the timeline is complete without an extra fetch.
    """
    repositories = dependencies.for_session(db)
    try:
        grant = await break_glass.grant_break_glass(
            break_glass.GrantBreakGlassCommand(
                tenant_id=user.tenant_id,
                actor=user,
                correlation_id=correlation.correlation_id,
                target_user_id=body.target_user_id,
                scope=body.scope,
                reason=body.reason,
                duration_minutes=body.duration_minutes,
            ),
            grant_repo=repositories.grant,
            audit=repositories.audit,
            clock=clock,
            new_id=uuid4(),
        )
    except Forbidden as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ValidationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    await db.commit()
    return _grant_to_response(grant)


@router.delete(
    "/break-glass/{grant_id}",
    response_model=BreakGlassGrantResponse,
    summary="Revoke a previously-issued break-glass grant.",
)
async def delete_break_glass_grant(
    grant_id: UUID = Path(...),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: SecurityHttpDependencyProvider = Depends(get_security_dependencies),
) -> BreakGlassGrantResponse:
    """Revoke ``grant_id`` if it exists in the actor's tenant.

    Self-revocation is permitted so an actor can shorten their own
    window without involving a second administrator. Idempotent:
    revoking an already-revoked grant succeeds and writes a flag
    row so the timeline shows the second attempt.
    """
    repositories = dependencies.for_session(db)
    try:
        grant = await break_glass.revoke_break_glass(
            break_glass.RevokeBreakGlassCommand(
                tenant_id=user.tenant_id,
                actor=user,
                correlation_id=correlation.correlation_id,
                grant_id=grant_id,
            ),
            grant_repo=repositories.grant,
            audit=repositories.audit,
            clock=clock,
        )
    except Forbidden as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await db.commit()
    return _grant_to_response(grant)


# ---------------------------------------------------------------------------
# Read-side enforcement (P06-US9 / FM-035)
#
# The only legitimate paths that cross a tenant boundary on reads.
# Both require an active grant in the target (data-owning) tenant,
# deny with the structured reason otherwise, and flag every success
# in audit plus the ``X-Break-Glass-Grant`` response header. No role
# check runs here — the grant IS the authorization (an auditor may
# hold no role in the target tenant by design).
# ---------------------------------------------------------------------------


class BreakGlassSearchRequest(BaseModel):
    """Wire shape for ``POST /security/break-glass/search``."""

    target_tenant_id: UUID
    query: str | None = Field(default=None, max_length=500)
    filters: list[SearchFilterRequest] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


def _break_glass_hit_to_wire(hit) -> CaseSearchHit:
    """Translate a search hit (mirrors the standard search router)."""
    return CaseSearchHit(
        id=hit.case_id,
        reference=hit.reference,
        priority=hit.priority,
        status=hit.status,
        title=hit.title,
        data_snippet=hit.data_snippet,
    )


def _break_glass_filters(
    filters: list[SearchFilterRequest],
    searchable_keys: tuple[str, ...],
) -> tuple[SearchFilter, ...]:
    """Translate wire filters, rejecting bad shapes with a 400.

    Mirrors the standard search path: unknown operators and filters
    on non-searchable fields are client bugs. Core case columns are
    always searchable; ``data.<key>`` filters must name a key the
    target tenant declared searchable.
    """
    searchable = set(searchable_keys) | set(case_search.CORE_FIELDS)
    out: list[SearchFilter] = []
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
        key = f.field[len("data.") :] if f.field.startswith("data.") else f.field
        if key not in searchable:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "validation_failed",
                    "message": f"field {f.field!r} is not a searchable field",
                },
            )
        out.append(SearchFilter(field=f.field, op=f.op, value=f.value))
    return tuple(out)


async def _break_glass_case_shape(
    *,
    target_tenant_id: UUID,
    case,
    case_dependencies: CaseHttpDependencyProvider,
    db: AsyncSession,
    user: UserContext,
) -> CaseSummarySchema:
    """Render the standard workspace summary for a break-glass read.

    Mirrors ``GET /cases/{id}`` (type key, schema-driven redaction)
    but resolves everything in the *target* tenant. Sensitive
    redaction follows the actor's *home* policy: the grant overrides
    the read boundary, not the field-level grant. SLA clocks are
    omitted — the override surface returns content, not timers.
    """
    repositories = case_dependencies.for_session(db)
    version = await repositories.case_type_version.get(target_tenant_id, case.case_type_version_id)
    case_type_key = ""
    is_sensitive = False
    schema = None
    if version is not None:
        case_type = await repositories.case_type.get(target_tenant_id, version.case_type_id)
        if case_type is not None:
            case_type_key = case_type.key
            is_sensitive = case_type.sensitivity_default in {"restricted", "phi"}
        schema = version.schema
    can_see_sensitive = await view_case_uc.actor_can_see_sensitive(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=case.id,
    )
    summary = build_summary(
        case=case,
        case_type_key=case_type_key,
        sensitive=is_sensitive,
        schema=schema,
        can_see_sensitive=can_see_sensitive,
        sla_state=None,
    )
    return CaseSummarySchema.model_validate(summary_to_dict(summary))


@router.get(
    "/break-glass/cases/{case_id}",
    response_model=CaseSummarySchema,
    summary=(
        "Read a case in another tenant under an active break-glass "
        "grant (P11-US10 / FM-035). The action is audit-flagged."
    ),
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Target is the actor's own tenant"},
        status.HTTP_403_FORBIDDEN: {"description": "No active grant for this scope"},
        status.HTTP_404_NOT_FOUND: {"description": "Case not found in target tenant"},
    },
)
async def read_case_under_break_glass(
    case_id: UUID,
    response: Response,
    target_tenant_id: UUID = Query(description="Data-owning tenant holding the grant"),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: SecurityHttpDependencyProvider = Depends(get_security_dependencies),
    case_dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
) -> CaseSummarySchema:
    """Read ``case_id`` from ``target_tenant_id`` under an override.

    Requires an active ``cross_tenant_case_read`` grant for the actor
    in the target tenant (minted by that tenant's administrator via
    ``POST /security/break-glass``). The flagged audit row carries
    the grant id, scope, and reason; the response header echoes the
    grant id so the override is prominent on the wire too.
    """
    repositories = dependencies.for_session(db)
    try:
        result = await break_glass.read_case_under_break_glass(
            target_tenant_id=target_tenant_id,
            actor=user,
            case_id=case_id,
            correlation_id=correlation.correlation_id,
            grant_repo=repositories.grant,
            case_repo=case_dependencies.for_session(db).case,
            audit=repositories.audit,
            clock=clock,
        )
    except ValidationFailed as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Forbidden as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except NotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await db.commit()
    response.headers["X-Break-Glass-Grant"] = str(result.grant.id)
    return await _break_glass_case_shape(
        target_tenant_id=target_tenant_id,
        case=result.case,
        case_dependencies=case_dependencies,
        db=db,
        user=user,
    )


@router.post(
    "/break-glass/search",
    response_model=CaseSearchResponse,
    summary=(
        "Search cases in another tenant under an active break-glass "
        "grant (P06-US9 / FM-035). The action is audit-flagged."
    ),
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Target is the actor's own tenant"},
        status.HTTP_403_FORBIDDEN: {"description": "No active grant for this scope"},
    },
)
async def search_under_break_glass(
    body: BreakGlassSearchRequest,
    response: Response,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: SecurityHttpDependencyProvider = Depends(get_security_dependencies),
    search_dependencies: SearchHttpDependencyProvider = Depends(get_search_dependencies),
) -> CaseSearchResponse:
    """Search ``target_tenant_id`` under a ``cross_tenant_search`` grant.

    Field-key sets resolve from the *target* tenant's published
    schemas (same P05 contract as the standard path); sensitive
    snippets follow the actor's *home* sensitive-read grant. The
    flagged audit row carries the grant id, scope, reason, and
    result count.
    """
    repositories = dependencies.for_session(db)
    search_repositories = search_dependencies.for_session(db)
    searchable_keys, sensitive_keys = await search_repositories.field_keys.field_keys(
        body.target_tenant_id
    )
    port_filters = _break_glass_filters(body.filters, searchable_keys)
    can_see_sensitive = await view_case_uc.actor_can_see_sensitive(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        # Placeholder: the grant is tenant-scoped, not case-scoped
        # (mirrors the standard search path).
        case_id=uuid4(),
    )
    try:
        result = await break_glass.search_cases_under_break_glass(
            target_tenant_id=body.target_tenant_id,
            actor=user,
            text_query=body.query,
            filters=port_filters,
            limit=body.limit,
            offset=body.offset,
            searchable_field_keys=searchable_keys,
            sensitive_field_keys=sensitive_keys,
            can_see_sensitive=can_see_sensitive,
            correlation_id=correlation.correlation_id,
            grant_repo=repositories.grant,
            search_provider=search_repositories.search,
            audit=repositories.audit,
            clock=clock,
        )
    except ValidationFailed as exc:
        # Filter-shape failures surface as 400 like the standard path;
        # same-tenant misuse is also a 400 (fail-closed direction).
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Forbidden as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    await db.commit()
    response.headers["X-Break-Glass-Grant"] = str(result.grant.id)
    return CaseSearchResponse(
        items=[_break_glass_hit_to_wire(h) for h in result.page.items],
        next_cursor=result.page.next_cursor,
        limit=result.page.limit,
        total_considered=result.page.total_considered,
    )


__all__ = ["router"]
