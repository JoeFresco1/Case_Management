"""Case intake route: POST /api/v1/cases (FR-001, P01-US1 / FM-005).

# INTEGRATION_SEAM: HTTP entry

POST ``/api/v1/cases`` is the canonical entry point for P01-US1 /
FM-005. It requires an ``Idempotency-Key`` header (FR-001,
AGENTS.md §10) and delegates to
``app.application.cases.create_case.handle``, which performs
authorization, validation, and the atomic case+audit+outbox write.

The middleware layer (``IdempotencyMiddleware``) handles replay; the
handler does not need to know about keys. The response projects the
canonical ``CaseSummary`` shape built by :func:`build_summary` — the
same shape GET / PATCH return.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases import create_case as create_case_uc
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
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.schemas.cases import (
    CaseSummary,
    CreateCaseRequest,
)

from .common import (
    _load_sla_state,
    _resolve_case_type_key,
    _resolve_case_type_schema,
    _summary_response,
    router,
)

# ---------------------------------------------------------------------------
# POST /api/v1/cases (FR-001, P01-US1)
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CaseSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a case",
)
async def create_case(
    body: CreateCaseRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    correlation_id: str = Header(default="", alias="X-Correlation-Id"),
) -> CaseSummary:
    # FR-001: POST /api/v1/cases requires an Idempotency-Key header. We
    # reject with 400 before opening the transaction so a missing key
    # never reaches the use case.
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    repositories = dependencies.for_session(db)
    case = await create_case_uc.handle(
        create_case_uc.CreateCaseCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            case_type_key=body.case_type_key,
            title=body.title,
            description=body.description,
            data=body.data,
            priority=body.priority,
            correlation_id=correlation_id,
        ),
        case_repo=repositories.case,
        case_type_repo=repositories.case_type,
        case_type_version_repo=repositories.case_type_version,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
        workflow=create_case_uc.WorkflowStarter(
            workflow_repo=repositories.workflow_definition,
            workflow_version_repo=repositories.workflow_version,
            instance_repo=repositories.workflow_instance,
            task_repo=repositories.task,
            sla_clock_repo=repositories.sla_clock,
        ),
    )
    await db.commit()
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
