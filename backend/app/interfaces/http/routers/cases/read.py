"""Case read routes: GET /api/v1/cases/{id} and GET /api/v1/cases.

# INTEGRATION_SEAM: HTTP entry

GET ``/api/v1/cases/{id}`` (P01-US1 / FM-006) and GET
``/api/v1/cases`` (P01-US2 / FR-002) return the canonical
``CaseSummary`` shape. Authorization is evaluated inside the use case
(``Action.CASE_READ``); the router only translates between Pydantic and
the application layer.

The workspace read resolves the coarse case-type sensitivity flag, the
published field schema, and the actor's sensitive-read grant so the
redactor can replace values (P01-US4 / P11-US6 / P05-US3), plus the SLA
badge state (P02-US6 / FM-018). The list read repeats that projection
per row (FM-011) so an unauthorized actor sees ref markers, never
values.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases import list_cases as list_cases_uc
from app.application.cases import view_case as view_case_uc
from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.application.cases.summary import build_summary
from app.deps import (
    Clock,
    UserContext,
    get_clock,
    get_current_user,
    get_db,
)
from app.domain.errors import ValidationFailed
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.schemas.cases import (
    CaseListItem,
    CaseSummary,
    Page,
)

from .common import (
    _list_item_response,
    _load_sla_state,
    _resolve_case_type_key,
    _resolve_case_type_schema,
    _summary_response,
    router,
)

# ---------------------------------------------------------------------------
# GET /api/v1/cases/{id} (P01-US1, FM-006)
# ---------------------------------------------------------------------------


@router.get(
    "/{case_id}",
    response_model=CaseSummary,
    summary="View a case (canonical workspace read shape)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
        status.HTTP_404_NOT_FOUND: {"description": "Case not found in tenant"},
    },
)
async def view_case(
    case_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
) -> CaseSummary:
    repositories = dependencies.for_session(db)
    case = await view_case_uc.handle(
        view_case_uc.ViewCaseQuery(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            case_id=case_id,
        ),
        case_repo=repositories.case,
    )

    case_type_key, is_sensitive = await _resolve_case_type_key(
        tenant_id=user.tenant_id,
        case_type_version_id=case.case_type_version_id,
        dependencies=dependencies,
        db=db,
    )
    schema = await _resolve_case_type_schema(
        tenant_id=user.tenant_id,
        case_type_version_id=case.case_type_version_id,
        dependencies=dependencies,
        db=db,
    )
    can_see_sensitive = await view_case_uc.actor_can_see_sensitive(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=case.id,
    )
    sla_state = await _load_sla_state(
        tenant_id=user.tenant_id,
        case_id=case.id,
        clock=clock,
        dependencies=dependencies,
        db=db,
    )
    summary = build_summary(
        case=case,
        case_type_key=case_type_key,
        sensitive=is_sensitive,
        schema=schema,
        can_see_sensitive=can_see_sensitive,
        sla_state=sla_state,
    )
    return _summary_response(summary)


# ---------------------------------------------------------------------------
# GET /api/v1/cases (P01-US2, FR-002)
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=Page[CaseListItem],
    summary="List cases for the current tenant (most recent first)",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
    },
)
async def list_cases(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[CaseListItem]:
    repositories = dependencies.for_session(db)
    try:
        cases = await list_cases_uc.handle(
            list_cases_uc.ListCasesQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                limit=limit,
                offset=offset,
            ),
            case_repo=repositories.case,
        )
    except ValidationFailed as exc:
        # The use case rejects negative offsets / non-positive limits;
        # the Pydantic Query(ge=...) guard already catches these for
        # callers that go through FastAPI. The translation is here for
        # safety and to keep the contract honest.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    items: list[CaseListItem] = []
    for case in cases:
        version = await repositories.case_type_version.get(
            case.tenant_id, case.case_type_version_id
        )
        if version is None:
            case_type_key, _ = "", False
            schema = None
        else:
            ct = await repositories.case_type.get(case.tenant_id, version.case_type_id)
            case_type_key = ct.key if ct is not None else ""
            # why: the list view previously ignored field-level
            # sensitivity (FM-011) and returned raw ``data`` for every
            # row. Resolve the coarse flag + schema + grant per row so
            # an unauthorized actor sees ref markers, never values.
            _is_sensitive = ct is not None and ct.sensitivity_default in {"restricted", "phi"}
            schema = version.schema
            can_see = await view_case_uc.actor_can_see_sensitive(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                case_id=case.id,
            )
            summary = build_summary(
                case=case,
                case_type_key=case_type_key,
                sensitive=_is_sensitive,
                schema=schema,
                can_see_sensitive=can_see,
            )
            items.append(_list_item_response(summary))
            continue
        summary = build_summary(case=case, case_type_key=case_type_key)
        items.append(_list_item_response(summary))

    # Cursor encoding: opaque ``<offset>:<limit>`` string. The next page
    # request is the caller's responsibility — they re-issue
    # ``GET /cases?limit=..&offset=next_offset`` (AGENTS.md §17.1 keeps
    # it simple for the first slice). A real cursor scheme (HMAC-signed
    # token) lands with FM-031 search.
    next_cursor: str | None = None
    if len(cases) == limit:
        next_cursor = f"{offset + limit}:{limit}"

    return Page[CaseListItem](items=items, next_cursor=next_cursor, limit=limit)
