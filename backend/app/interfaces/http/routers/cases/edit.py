"""Case edit route: PATCH /api/v1/cases/{id} (P01-US3, FR-003, FM-007).

# INTEGRATION_SEAM: HTTP entry

Optimistic concurrency via the ``If-Match`` header carrying the case's
``version_token``; a stale token returns 409 with the current token in
the response headers so the client can refetch. Delegates to
``app.application.cases.edit_case.handle`` which performs
authorization, validation against the case type schema, field history,
and the atomic case+audit+outbox write.

The response is the canonical ``CaseSummary`` shape (same as POST /
GET) so the client can render the workspace without a second
round-trip, with an ``ETag`` the next PATCH can quote as ``If-Match``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases import edit_case as edit_case_uc
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
from app.domain.errors import Conflict
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.schemas.cases import (
    CaseSummary,
    EditCaseRequest,
)

from .common import (
    _load_sla_state,
    _resolve_case_type_key,
    _resolve_case_type_schema,
    _summary_response,
    router,
)

# ---------------------------------------------------------------------------
# PATCH /api/v1/cases/{id} (P01-US3, FR-003, FM-007)
# ---------------------------------------------------------------------------


@router.patch(
    "/{case_id}",
    response_model=CaseSummary,
    summary="Edit a case (optimistic concurrency + field history)",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "If-Match header is required or malformed",
        },
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.update"},
        status.HTTP_404_NOT_FOUND: {"description": "Case not found in tenant"},
        status.HTTP_409_CONFLICT: {
            "description": "Stale If-Match token; body carries the current token",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Validation failed against the case type schema",
        },
    },
)
async def edit_case(
    case_id: UUID,
    body: EditCaseRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    if_match: str | None = Header(default=None, alias="If-Match"),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),
) -> CaseSummary:
    # FR-003: PATCH /api/v1/cases/{id} requires an ``If-Match`` header
    # carrying the ``version_token`` the caller read from the prior GET.
    # A missing or non-integer token is rejected with 400 so it never
    # reaches the use case.
    if not if_match or not if_match.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="If-Match header is required (use the case's version_token)",
        )
    try:
        expected_version_token = int(if_match.strip().strip('"'))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="If-Match header must be an integer version_token",
        ) from exc

    repositories = dependencies.for_session(db)
    try:
        result = await edit_case_uc.handle(
            edit_case_uc.EditCaseCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                case_id=case_id,
                expected_version_token=expected_version_token,
                title=body.title,
                description=body.description,
                priority=body.priority,
                data=body.data,
                correlation_id=correlation_id,
            ),
            case_repo=repositories.case,
            case_type_version_repo=repositories.case_type_version,
            audit=repositories.audit,
            outbox=repositories.outbox,
            clock=clock,
        )
    except Conflict as exc:
        # FR-003 acceptance scenario 3: a stale token returns 409 with
        # the current token in the body so the client can refetch. The
        # error handler in ``interfaces/http/errors.py`` already maps
        # ``Conflict`` to 409 with the message; we attach the current
        # ``version_token`` via ``details`` so the client does not have
        # to do a second GET to recover. The token comes from a fresh
        # tenant-scoped read so a cross-tenant id never leaks the
        # other tenant's token.
        current = await repositories.case.get(user.tenant_id, case_id)
        current_token = current.version_token if current is not None else None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
            headers={
                "ETag": f'"{current_token}"' if current_token is not None else "",
                "X-Current-Version-Token": (
                    str(current_token) if current_token is not None else ""
                ),
            },
        ) from exc
    await db.commit()

    # Return the canonical summary shape (same as POST / GET) so the
    # client can render the workspace without a second round-trip.
    case_type_key, is_sensitive = await _resolve_case_type_key(
        tenant_id=user.tenant_id,
        case_type_version_id=result.case.case_type_version_id,
        dependencies=dependencies,
        db=db,
    )
    schema = await _resolve_case_type_schema(
        tenant_id=user.tenant_id,
        case_type_version_id=result.case.case_type_version_id,
        dependencies=dependencies,
        db=db,
    )
    can_see_sensitive = await view_case_uc.actor_can_see_sensitive(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=result.case.id,
    )
    sla_state = await _load_sla_state(
        tenant_id=user.tenant_id,
        case_id=result.case.id,
        clock=clock,
        dependencies=dependencies,
        db=db,
    )
    summary = build_summary(
        case=result.case,
        case_type_key=case_type_key,
        sensitive=is_sensitive,
        schema=schema,
        can_see_sensitive=can_see_sensitive,
        sla_state=sla_state,
    )
    response = _summary_response(summary)
    # Set the ETag so the client's next PATCH can quote it as If-Match
    # without an extra GET (RFC 7232 §2.3).
    response.headers["ETag"] = f'"{result.case.version_token}"'
    return response
