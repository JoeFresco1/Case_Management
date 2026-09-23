"""Operational KPI dashboard route (P12-US8 / FM-054).

Registers ``GET /api/v1/reports/dashboards/operational`` onto the
package :mod:`common` reports ``router`` (the router prefix makes the
wire path the "dashboards" surface the spec text describes; the
operational-report home stays this package).

The endpoint combines the open-cases and overdue-tasks snapshots into a
single response with an explicit ``generated_at`` source timestamp and a
``freshness_seconds`` field (FM-054 promises sub-five-second freshness).
Authorization is delegated to the underlying snapshot reads in
:mod:`app.application.reports.dashboards` — the supervisor role grants
both ``case.read`` and ``task.read``, so no extra check is repeated here.

# INTEGRATION_SEAM: report engine

Same seam as :mod:`operational`: the use case is the swap point for an
Azure Synapse / Power BI ``Report`` port; the wire shape already carries
``kind`` / ``generated_at`` / ``team``.
"""

from __future__ import annotations

from fastapi import Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.reports import dashboards
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
    OperationalDashboardResponse,
    OperationalKPIsResponse,
)

from .common import _translate_error, router


def _dashboard_to_wire(
    dashboard: dashboards.OperationalDashboard,
) -> OperationalDashboardResponse:
    """Translate the use-case dataclass to the wire schema.

    Why centralized: the Pydantic schema is the OpenAPI contract;
    hand-editing fields at a route is forbidden (AGENTS.md §4.R7).

    Args:
        dashboard: Application-layer operational dashboard dataclass.

    Returns:
        The wire ``OperationalDashboardResponse``.
    """
    return OperationalDashboardResponse(
        kind=dashboard.kind,
        generated_at=dashboard.generated_at,
        served_at=dashboard.served_at,
        freshness_seconds=dashboard.freshness_seconds,
        tenant_id=dashboard.tenant_id,
        actor_id=dashboard.actor_id,
        team=dashboard.team,
        kpis=OperationalKPIsResponse(
            total_open_cases=dashboard.kpis.total_open_cases,
            total_overdue_tasks=dashboard.kpis.total_overdue_tasks,
            open_by_priority=dict(dashboard.kpis.open_by_priority),
        ),
    )


@router.get(
    "/dashboards/operational",
    response_model=OperationalDashboardResponse,
    summary="Operational KPI dashboard for the actor's team (P12-US8)",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks case.read or task.read",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "SQL adapter not yet wired (T063)",
        },
    },
)
async def operational_dashboard(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ReportsHttpDependencyProvider = Depends(get_reports_dependencies),
    clock: Clock = Depends(get_clock),
) -> OperationalDashboardResponse:
    """Return the manager KPI dashboard for the actor's team.

    Mounted under the reports prefix so the wire path resolves to
    ``GET /api/v1/reports/dashboards/operational`` (the spec text
    calls it the "dashboards" surface; the router module is the
    operational-report home). Combines the open-cases and overdue-tasks
    snapshots into a single response with an explicit ``generated_at``
    source timestamp and a ``freshness_seconds`` field (FM-054
    promises sub-five-second freshness). Authorization is delegated to
    the underlying snapshot reads; the supervisor role grants both
    required actions.

    Args:
        user: Authenticated caller (identity + tenant + roles).
        db: Request-scoped async DB session.
        dependencies: Reports repository bundle for this session.
        clock: Injectable clock (testability).

    Returns:
        The KPI dashboard for the actor's team.

    Raises:
        HTTPException: 403 when a snapshot read is denied, 503 when the
            SQL adapter is unavailable.
    """
    repositories = dependencies.for_session(db)
    case_repo = repositories.case
    task_repo = repositories.task
    queue_repo = repositories.queue
    now = clock.now()
    try:
        dashboard = await dashboards.operational_dashboard(
            dashboards.OperationalDashboardQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
            ),
            case_repo=case_repo,
            task_repo=task_repo,
            queue_repo=queue_repo,
            now=now,
            served_at=clock.now(),
        )
    except (Forbidden, ValidationFailed, DependencyUnavailable) as exc:
        raise _translate_error(exc) from exc
    return _dashboard_to_wire(dashboard)
