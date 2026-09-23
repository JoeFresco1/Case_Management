"""Operational reports + custom report designer router (P12 / FM-051, FM-053, FM-054).

# Why a package (R2 structural split)

Formerly a single ``reports.py`` module of 722 lines — past the R2
600-line soft limit. Split by route-group responsibility into
:mod:`common` (shared ``router``, ``Idempotency-Key`` guard, the two
domain-error translations), :mod:`operational` (``GET /open-cases`` +
``GET /overdue-tasks``), :mod:`custom` (define / render / export a
custom report), and :mod:`dashboard`` (``GET /dashboards/operational``).
This ``__init__`` is a pure re-export shim: ``__all__`` and every
previously visible attribute — public and private, including names the
old module merely imported, such as ``Annotated``/``status``/
``custom_reports`` — are re-exported unchanged, so no consumer import
line needs editing (``from ...reports import router`` in
``app/main.py:188`` and ``tests/interfaces/test_records_reports_contracts.py:62``
keeps working, as does
``mock.patch("app.interfaces.http.routers.reports.X")``). The route set
and the OpenAPI operation ids are unchanged.

Mounts the following read-only / write-once endpoints:

- ``GET /api/v1/reports/open-cases`` — the snapshot of open cases
  for the supervisor's team.
- ``GET /api/v1/reports/overdue-tasks`` — the snapshot of overdue
  tasks for the supervisor's team.
- ``POST /api/v1/reports/custom-definitions`` — process
  administrator declares a controlled custom report spec
  (groupings, aggregations, filters, time window).
- ``GET /api/v1/reports/custom/{definition_id}`` — render a custom
  report definition against the operational read models.
- ``POST /api/v1/reports/custom/{definition_id}/export`` — render
  the custom report and return an audited CSV body.
- ``GET /api/v1/reports/dashboards/operational`` — the manager KPI
  dashboard for the supervisor's team.

The operational endpoints delegate to
:mod:`app.application.reports.operational_reports` which authorizes
the actor against ``Action.CASE_READ`` (deny by default) and
resolves the actor's team from the live queue membership set. The
custom report endpoints delegate to
:mod:`app.application.reports.custom_reports` which additionally
enforces ``ADMIN_TENANT_CONFIG`` (define) and ``AUDIT_EXPORT``
(export) and stages a single ``AuditExported`` audit row in the
same transaction as the export read.

# Authorization (P11)

The operational endpoints require ``case.read`` (the supervisor role
grants this along with ``case.read_sensitive``). A reviewer who is
not a queue member sees an empty report — the report never confirms
whether a queue exists in another team. A missing role surfaces 403.

The custom-definition endpoint requires ``admin.tenant_config`` (the
process administrator role). The export endpoint additionally
requires ``audit.export`` because the export action is itself a
security-relevant audit event.

# INTEGRATION_SEAM: report engine

A future slice will swap the application-level use cases for a
``Report`` port backed by Azure Synapse / Power BI. The wire shape
already carries ``kind`` / ``generated_at`` / ``team`` so the swap
is a single-file change at this router package.
"""

from __future__ import annotations

from .common import (  # noqa: F401
    APIRouter,
    DependencyUnavailable,
    Forbidden,
    HTTPException,
    NotFound,
    ValidationFailed,
    _require_idempotency_key,
    _translate_error,
    _translate_not_found,
    router,
    status,
)
from .custom import (  # noqa: F401
    UUID,
    Action,
    Annotated,
    AsyncSession,
    Clock,
    CorrelationContext,
    CustomReportBucketResponse,
    CustomReportDefinitionCreate,
    CustomReportDefinitionResponse,
    CustomReportResultResponse,
    Depends,
    FilterClauseSchema,
    Header,
    Query,
    ReportExportResponse,
    ReportsHttpDependencyProvider,
    Resource,
    SystemClock,
    UserContext,
    _coerce_create,
    _custom_report_to_wire,
    _definition_to_wire,
    _stamp_actor,
    access_audit_uc,
    authorize,
    create_custom_report_definition,
    custom_reports,
    export_custom_report,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_custom_report,
    get_db,
    get_reports_dependencies,
    operational_reports,
)
from .dashboard import (  # noqa: F401
    OperationalDashboardResponse,
    OperationalKPIsResponse,
    _dashboard_to_wire,
    dashboards,
    operational_dashboard,
)
from .operational import (  # noqa: F401
    OpenCaseReportRow,
    OpenCasesReportResponse,
    OverdueTaskReportRow,
    OverdueTasksReportResponse,
    _open_cases_to_wire,
    _overdue_to_wire,
    open_cases,
    overdue_tasks,
)

__all__ = ["router"]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, and packages additionally carry ``__path__`` — neither
# existed on the pre-split single-file module. Popping them keeps
# ``dir(app.interfaces.http.routers.reports)`` byte-identical to the old
# module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (``...reports.operational`` resolves through
# ``sys.modules``, which is untouched); only the redundant package-level
# attributes go away.
for _submodule in (
    "common",
    "custom",
    "dashboard",
    "operational",
    "__path__",
):
    globals().pop(_submodule, None)
del _submodule
