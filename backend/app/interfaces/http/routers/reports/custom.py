"""Custom report designer routes — define, render, audited CSV export (P12-US6, US7 / FM-053).

Registers the three custom-report endpoints onto the package
:mod:`common` reports ``router``:

- ``POST /api/v1/reports/custom-definitions`` — a process
  administrator declares a controlled custom report spec (groupings,
  aggregations, filters, time window).
- ``GET /api/v1/reports/custom/{definition_id}`` — render a custom
  report definition against the operational read models.
- ``POST /api/v1/reports/custom/{definition_id}/export`` — render the
  custom report and return an audited CSV body.

The endpoints delegate to
:mod:`app.application.reports.custom_reports`, which additionally
enforces ``ADMIN_TENANT_CONFIG`` (define) and ``AUDIT_EXPORT`` (export)
and stages a single ``AuditExported`` audit row in the same transaction
as the export read.

# Authorization (P11)

The definition endpoint requires ``admin.tenant_config`` (the process
administrator role). The export endpoint additionally requires
``audit.export`` because the export action is itself a security-relevant
audit event. The render endpoint reuses the operational reports'
team-scope contract (``case.read``) so a custom report cannot widen the
actor's team set.

# Idempotency

Both mutations require ``Idempotency-Key`` (AGENTS.md §10): the router
rejects a missing/blank key with 400 before the use case runs, and key
reuse with a different payload surfaces as 409 via the global
``IdempotencyMiddleware``.

# INTEGRATION_SEAM: report engine

Same seam as :mod:`operational`: the renderer and exporter reach the
system of record only through application-layer repositories, so an
Azure Synapse / Power BI swap lands at the application port, not here.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import access_audit as access_audit_uc
from app.application.authorization import Action, Resource, authorize
from app.application.reports import custom_reports, operational_reports
from app.application.reports.http_dependencies import (
    ReportsHttpDependencyProvider,
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
from app.domain.errors import (
    DependencyUnavailable,
    Forbidden,
    NotFound,
    ValidationFailed,
)
from app.interfaces.http.dependencies import get_reports_dependencies
from app.interfaces.http.schemas.reports import (
    CustomReportBucketResponse,
    CustomReportDefinitionCreate,
    CustomReportDefinitionResponse,
    CustomReportResultResponse,
    FilterClauseSchema,
    ReportExportResponse,
)

from .common import _require_idempotency_key, _translate_not_found, router


def _definition_to_wire(
    definition: custom_reports.ReportDefinition,
) -> CustomReportDefinitionResponse:
    """Translate a :class:`ReportDefinition` to the wire shape.

    Why centralized: the Pydantic schema is the OpenAPI contract;
    hand-editing fields at a route is forbidden (AGENTS.md §4.R7).

    Args:
        definition: Application-layer report definition dataclass.

    Returns:
        The wire ``CustomReportDefinitionResponse``.
    """
    return CustomReportDefinitionResponse(
        id=definition.id,
        tenant_id=definition.tenant_id,
        key=definition.key,
        name=definition.name,
        description=definition.description,
        group_by=tuple(gb.value for gb in definition.group_by),
        aggregations=tuple(agg.value for agg in definition.aggregations),
        filters=tuple(
            FilterClauseSchema(field=c.field, op=c.op.value, value=c.value)
            for c in definition.filters
        ),
        time_window_hours=definition.time_window_hours,
        created_by=definition.created_by,
        created_at=definition.created_at,
        version_token=definition.version_token,
    )


def _custom_report_to_wire(
    report: custom_reports.CustomReportResult,
) -> CustomReportResultResponse:
    """Translate a rendered custom report to the wire shape.

    Args:
        report: Application-layer custom report result dataclass.

    Returns:
        The wire ``CustomReportResultResponse``.
    """
    return CustomReportResultResponse(
        kind=report.kind.value,
        generated_at=report.generated_at,
        tenant_id=report.tenant_id,
        actor_id=report.actor_id,
        definition_id=report.definition_id,
        definition_key=report.definition_key,
        definition_name=report.definition_name,
        total=report.total,
        items=tuple(
            CustomReportBucketResponse(
                key=bucket.key,
                count=bucket.count,
                overdue_seconds=bucket.overdue_seconds,
            )
            for bucket in report.items
        ),
    )


def _coerce_create(body: CustomReportDefinitionCreate) -> custom_reports.DefineCustomReportCommand:
    """Translate the wire create payload into the application command.

    The closed-vocabulary validators run inside :func:`define_custom_report`;
    we coerce strings to the StrEnum types here so the router does not
    need to know about the enum shapes. A bad enum value surfaces as a
    Pydantic validation error before reaching the use case.

    Args:
        body: Validated wire create payload.

    Returns:
        The application command with placeholder actor fields.

    Raises:
        ValueError: when a group-by / aggregation / filter value is
            outside the closed vocabulary (mapped to 422 by the route).
    """
    return custom_reports.DefineCustomReportCommand(
        tenant_id=UUID("00000000-0000-0000-0000-000000000000"),
        actor_id=UUID("00000000-0000-0000-0000-000000000000"),
        actor_roles=(),
        key=body.key,
        name=body.name,
        description=body.description,
        group_by=tuple(custom_reports.GroupByField(value) for value in body.group_by),
        aggregations=tuple(custom_reports.AggregationKind(value) for value in body.aggregations),
        filters=tuple(
            custom_reports.FilterClause(
                field=c.field,
                op=custom_reports.FilterOperator(c.op),
                value=c.value,
            )
            for c in body.filters
        ),
        time_window_hours=body.time_window_hours,
    )


def _stamp_actor(
    cmd: custom_reports.DefineCustomReportCommand,
    *,
    user: UserContext,
) -> custom_reports.DefineCustomReportCommand:
    """Bind the actor + tenant onto a previously coerced command.

    Why a second step: ``_coerce_create`` does not know the actor;
    the router threads the actor from the dependency after Pydantic
    has validated the wire body. Returning a new frozen dataclass
    keeps the validation chain pure.

    Args:
        cmd: Command produced by :func:`_coerce_create`.
        user: Authenticated caller (identity + tenant + roles).

    Returns:
        A copy of ``cmd`` stamped with the caller's tenant and roles.
    """
    return custom_reports.DefineCustomReportCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        key=cmd.key,
        name=cmd.name,
        description=cmd.description,
        group_by=cmd.group_by,
        aggregations=cmd.aggregations,
        filters=cmd.filters,
        time_window_hours=cmd.time_window_hours,
    )


@router.post(
    "/custom-definitions",
    response_model=CustomReportDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Define a custom report spec (P12-US7)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks admin.tenant_config",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Key reuse conflict, or duplicate definition key",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Spec violates the closed vocabulary",
        },
    },
)
async def create_custom_report_definition(
    body: CustomReportDefinitionCreate,
    user: UserContext = Depends(get_current_user),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CustomReportDefinitionResponse:
    """Persist a controlled declarative custom-report spec.

    The admin-only endpoint validates the body against the closed
    vocabulary and stamps the actor + tenant + clock onto the
    resulting definition so the audit chain always agrees with the
    row.

    Contracts: ``Idempotency-Key`` is required (transport retries replay
    via ``IdempotencyMiddleware``); key reuse with a different payload
    surfaces as 409 ``ErrorResponse{conflict}``.

    Args:
        body: Validated wire create payload.
        user: Authenticated caller (identity + tenant + roles).
        clock: Injectable clock (testability).
        idempotency_key: Raw ``Idempotency-Key`` header value.

    Returns:
        The persisted definition (201).

    Raises:
        HTTPException: 400 when the key is missing/blank, 403 when
            ``admin.tenant_config`` is missing, 422 when the spec
            violates the closed vocabulary, 409 on key/duplicate conflict.
    """
    _require_idempotency_key(idempotency_key)
    try:
        cmd = _stamp_actor(_coerce_create(body), user=user)
    except ValueError as exc:
        # why: the closed-vocabulary StrEnum coercion raises ValueError
        # (not ValidationFailed) for unknown group-by/aggregation/filter
        # values. Without this branch the error escapes as 500
        # internal_error; map it to 422 so the admin sees which vocabulary
        # term was rejected (FM-053 acceptance).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "validation_failed", "message": str(exc)},
        ) from exc
    try:
        definition = await custom_reports.define_custom_report(
            cmd,
            repo=custom_reports.default_repository(),
            clock=clock,
        )
    except (Forbidden, ValidationFailed, NotFound) as exc:
        raise _translate_not_found(exc) from exc
    return _definition_to_wire(definition)


@router.get(
    "/custom/{definition_id}",
    response_model=CustomReportResultResponse,
    summary="Render a custom report (P12-US7)",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks case.read or task.read",
        },
        status.HTTP_404_NOT_FOUND: {"description": "Definition not found in tenant"},
    },
)
async def get_custom_report(
    definition_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ReportsHttpDependencyProvider = Depends(get_reports_dependencies),
    clock: Clock = Depends(get_clock),
) -> CustomReportResultResponse:
    """Render the custom report definition against the system of record.

    The renderer delegates to the operational reports' team-scope
    contract so a custom report cannot widen the actor's team set.

    Args:
        definition_id: Definition id (tenant-scoped).
        user: Authenticated caller (identity + tenant + roles).
        db: Request-scoped async DB session.
        dependencies: Reports repository bundle for this session.
        clock: Injectable clock (testability).

    Returns:
        The rendered custom report.

    Raises:
        HTTPException: 404 when the definition is absent in this tenant,
            403 when the actor lacks the snapshot reads, 503 when the
            SQL adapter is unavailable.
    """
    repo = custom_reports.default_repository()
    definition = await repo.get(user.tenant_id, definition_id)
    if definition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": "report definition not found in tenant"},
        )
    repositories = dependencies.for_session(db)
    case_repo = repositories.case
    queue_repo = repositories.queue
    try:
        report = await custom_reports.render_custom_report(
            operational_reports.OperationalReportsQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
            ),
            definition,
            case_repo=case_repo,
            queue_repo=queue_repo,
            now=clock.now(),
        )
    except (Forbidden, ValidationFailed, NotFound, DependencyUnavailable) as exc:
        raise _translate_not_found(exc) from exc
    return _custom_report_to_wire(report)


@router.post(
    "/custom/{definition_id}/export",
    response_model=ReportExportResponse,
    summary="Export a custom report as CSV with audit trail (P12-US6)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks audit.export"},
        status.HTTP_404_NOT_FOUND: {"description": "Definition not found in tenant"},
        status.HTTP_409_CONFLICT: {"description": "Idempotency-Key reuse with a different payload"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Malformed definition"},
    },
)
async def export_custom_report(
    definition_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ReportsHttpDependencyProvider = Depends(get_reports_dependencies),
    clock: Clock = Depends(get_clock),
    correlation: CorrelationContext = Depends(get_correlation_id),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    reason: Annotated[
        str,
        Query(
            min_length=1,
            description=(
                "Free-text justification recorded on the AuditExported "
                "event this call appends to the chain."
            ),
        ),
    ] = "unspecified",
) -> ReportExportResponse:
    """Render and export a custom report as CSV; the export is audited.

    Authorization: ``audit.export`` is required (the export action
    is itself a security event). The use case stages a single
    ``AuditExported`` row in the same transaction; the router commits
    once.

    Contracts: ``Idempotency-Key`` is required; the audited export
    replays on transport retry instead of double-appending audit rows.

    Args:
        definition_id: Definition id (tenant-scoped).
        user: Authenticated caller (identity + tenant + roles).
        db: Request-scoped async DB session.
        dependencies: Reports repository bundle for this session.
        clock: Injectable clock (testability).
        correlation: Request correlation id context.
        idempotency_key: Raw ``Idempotency-Key`` header value.
        reason: Free-text justification for the audit event.

    Returns:
        CSV body plus the audit row id and source timestamp.

    Raises:
        HTTPException: 400 when the key is missing/blank, 403 when
            ``audit.export`` is missing, 404 when the definition is
            absent, 503 when a dependency is unavailable.
    """
    _require_idempotency_key(idempotency_key)
    repositories = dependencies.for_session(db)
    audit = repositories.audit
    await authorize(
        Action.AUDIT_EXPORT,
        user,
        Resource(tenant_id=user.tenant_id, kind="report_export"),
        access_recorder=access_audit_uc.build_recorder(
            audit=audit,
            clock=SystemClock(),
        ),
        correlation_id=correlation.correlation_id,
    )

    repo = custom_reports.default_repository()
    case_repo = repositories.case
    queue_repo = repositories.queue
    try:
        result = await custom_reports.export_custom_report(
            custom_reports.ExportCustomReportCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                definition_id=definition_id,
                reason=reason,
                correlation_id=correlation.correlation_id,
            ),
            repo=repo,
            case_repo=case_repo,
            queue_repo=queue_repo,
            audit=audit,
            dedupe=repositories.dedupe,
            clock=clock,
        )
    except (Forbidden, ValidationFailed, NotFound, DependencyUnavailable) as exc:
        raise _translate_not_found(exc) from exc

    return ReportExportResponse(
        audit_event_id=result.audit_row_id,
        generated_at=result.report.generated_at,
        definition_id=result.report.definition_id,
        row_count=len(result.report.items),
        csv_body=result.csv_body,
    )
