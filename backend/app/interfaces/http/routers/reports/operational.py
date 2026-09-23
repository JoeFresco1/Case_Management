"""Operational snapshot routes — open cases + overdue tasks (P12-US1, US2 / FM-051).

Registers the two read-only supervisor snapshots onto the package
:mod:`common` reports ``router``:

- ``GET /api/v1/reports/open-cases`` — the snapshot of open cases for
  the supervisor's team (P12-US1).
- ``GET /api/v1/reports/overdue-tasks`` — the snapshot of overdue tasks
  for the supervisor's team (P12-US2).

Both endpoints delegate to
:mod:`app.application.reports.operational_reports`, which authorizes the
actor against ``Action.CASE_READ`` (deny by default) and resolves the
actor's team from the live queue membership set.

# Authorization (P11)

The operational endpoints require ``case.read`` (the supervisor role
grants this along with ``case.read_sensitive``). A reviewer who is not a
queue member sees an empty report — the report never confirms whether a
queue exists in another team. A missing role surfaces 403.

The reports are **read-only**: no audit or outbox row is written (P12
pillar statement §2: "reports do not modify state"). The response
carries ``generated_at`` so the caller knows exactly when the system of
record was sampled.

# INTEGRATION_SEAM: report engine

A future slice will swap the application-level use cases for a
``Report`` port backed by Azure Synapse / Power BI. The wire shape
already carries ``kind`` / ``generated_at`` / ``team`` so the swap is a
single-file change at this package.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.reports import operational_reports
from app.application.reports.http_dependencies import (
    ReportsHttpDependencyProvider,
)
from app.deps import (
    Clock,
    UserContext,
    get_clock,
    get_current_user,
    get_db,
)
from app.domain.errors import DependencyUnavailable, Forbidden, ValidationFailed
from app.interfaces.http.dependencies import get_reports_dependencies
from app.interfaces.http.schemas.reports import (
    OpenCaseReportRow,
    OpenCasesReportResponse,
    OverdueTaskReportRow,
    OverdueTasksReportResponse,
)

from .common import _translate_error, router


def _open_cases_to_wire(report: operational_reports.OpenCasesReport) -> OpenCasesReportResponse:
    """Translate the open-cases use-case dataclass to the wire schema.

    Why centralized: the Pydantic schema is the OpenAPI contract;
    hand-editing fields at a route is forbidden (AGENTS.md §4.R7).

    Args:
        report: Application-layer open-cases report dataclass.

    Returns:
        The wire ``OpenCasesReportResponse``.
    """
    return OpenCasesReportResponse(
        kind=report.kind.value,
        generated_at=report.generated_at,
        tenant_id=report.tenant_id,
        actor_id=report.actor_id,
        team=report.team,
        total=report.total,
        items=tuple(
            OpenCaseReportRow(
                case_id=row.case_id,
                reference=row.reference,
                case_type_version_id=row.case_type_version_id,
                status=row.status,
                priority=row.priority,
                title=row.title,
                current_workflow_state=row.current_workflow_state,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in report.items
        ),
    )


def _overdue_to_wire(report: operational_reports.OverdueTasksReport) -> OverdueTasksReportResponse:
    """Translate the overdue-tasks use-case dataclass to the wire schema.

    Why centralized: the Pydantic schema is the OpenAPI contract;
    hand-editing fields at a route is forbidden (AGENTS.md §4.R7).

    Args:
        report: Application-layer overdue-tasks report dataclass.

    Returns:
        The wire ``OverdueTasksReportResponse``.
    """
    return OverdueTasksReportResponse(
        kind=report.kind.value,
        generated_at=report.generated_at,
        tenant_id=report.tenant_id,
        actor_id=report.actor_id,
        team=report.team,
        total=report.total,
        items=tuple(
            OverdueTaskReportRow(
                task_id=row.task_id,
                case_id=row.case_id,
                type=row.type,
                title=row.title,
                priority=row.priority,
                assignee_user_id=row.assignee_user_id,
                assignee_queue_id=row.assignee_queue_id,
                due_at=row.due_at,
                overdue_seconds=row.overdue_seconds,
            )
            for row in report.items
        ),
    )


@router.get(
    "/open-cases",
    response_model=OpenCasesReportResponse,
    summary="Open-cases snapshot for the actor's team (P12-US1)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "limit out of range"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "SQL adapter not yet wired (T063)",
        },
    },
)
async def open_cases(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ReportsHttpDependencyProvider = Depends(get_reports_dependencies),
    clock: Clock = Depends(get_clock),
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
) -> OpenCasesReportResponse:
    """Return the snapshot of open cases for the supervisor's team.

    The report is **read-only**; no audit or outbox row is written
    (P12 pillar statement §2: "reports do not modify state"). The
    response carries ``generated_at`` so the caller knows exactly
    when the system of record was sampled.

    Args:
        user: Authenticated caller (identity + tenant + roles).
        db: Request-scoped async DB session.
        dependencies: Reports repository bundle for this session.
        clock: Injectable clock (testability).
        limit: Page size (1..200; default 200).

    Returns:
        The open-cases snapshot for the actor's team.

    Raises:
        HTTPException: 403 when ``case.read`` is missing, 400 on an
            out-of-range limit, 503 when the SQL adapter is unavailable.
    """
    repositories = dependencies.for_session(db)
    case_repo = repositories.case
    queue_repo = repositories.queue
    try:
        report = await operational_reports.open_cases(
            operational_reports.OperationalReportsQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
            ),
            case_repo=case_repo,
            queue_repo=queue_repo,
            now=clock.now(),
            limit=limit,
        )
    except (Forbidden, ValidationFailed, DependencyUnavailable) as exc:
        raise _translate_error(exc) from exc
    return _open_cases_to_wire(report)


@router.get(
    "/overdue-tasks",
    response_model=OverdueTasksReportResponse,
    summary="Overdue-tasks snapshot for the actor's team (P12-US2)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "limit out of range"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks case.read"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "SQL adapter not yet wired (T063)",
        },
    },
)
async def overdue_tasks(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ReportsHttpDependencyProvider = Depends(get_reports_dependencies),
    clock: Clock = Depends(get_clock),
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
) -> OverdueTasksReportResponse:
    """Return the snapshot of overdue tasks for the supervisor's team.

    A task is overdue iff ``status == 'open'`` AND ``due_at <= now``.
    The report is scoped to the actor's queue memberships; a task
    assigned to a queue the supervisor does not belong to is invisible.

    Args:
        user: Authenticated caller (identity + tenant + roles).
        db: Request-scoped async DB session.
        dependencies: Reports repository bundle for this session.
        clock: Injectable clock (testability).
        limit: Page size (1..200; default 200).

    Returns:
        The overdue-tasks snapshot for the actor's team.

    Raises:
        HTTPException: 403 when ``case.read`` is missing, 400 on an
            out-of-range limit, 503 when the SQL adapter is unavailable.
    """
    repositories = dependencies.for_session(db)
    task_repo = repositories.task
    queue_repo = repositories.queue
    try:
        report = await operational_reports.overdue_tasks(
            operational_reports.OperationalReportsQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
            ),
            task_repo=task_repo,
            queue_repo=queue_repo,
            now=clock.now(),
            limit=limit,
        )
    except (Forbidden, ValidationFailed, DependencyUnavailable) as exc:
        raise _translate_error(exc) from exc
    return _overdue_to_wire(report)
