"""Operational report wire schemas (P12-US1, US2 / FM-051) and
custom report / export schemas (P12-US6, US7 / FM-053).

The schemas are the OpenAPI contract. The application-layer
dataclasses in :mod:`app.application.reports.operational_reports`
and :mod:`app.application.reports.custom_reports` own the runtime
shape; this module only mirrors what crosses the wire. New fields
land in the dataclass first, then in this schema — the router never
hand-types fields (AGENTS.md §4.R7).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OpenCaseReportRow(BaseModel):
    """One row of the open-cases report (P12-US1 / FM-051)."""

    model_config = ConfigDict(frozen=True)

    case_id: UUID
    reference: str
    case_type_version_id: UUID
    status: str
    priority: str
    title: str
    current_workflow_state: str | None = None
    created_at: datetime
    updated_at: datetime


class OpenCasesReportResponse(BaseModel):
    """The wire shape for ``GET /api/v1/reports/open-cases``.

    ``generated_at`` is the system-of-record-as-of timestamp (P12 §1).
    ``team`` is the sorted list of queue keys the supervisor is a
    member of; an empty list means the actor has no team.
    ``kind`` is always ``"snapshot"`` for the first slice.
    """

    model_config = ConfigDict(frozen=True)

    kind: str = Field(description='Report kind; always "snapshot" for the first slice.')
    generated_at: datetime
    tenant_id: UUID
    actor_id: UUID
    team: tuple[str, ...] = Field(
        description="Sorted queue keys the report covers (the actor's team).",
    )
    total: int
    items: tuple[OpenCaseReportRow, ...]


class OverdueTaskReportRow(BaseModel):
    """One row of the overdue-tasks report (P12-US2 / FM-051)."""

    model_config = ConfigDict(frozen=True)

    task_id: UUID
    case_id: UUID
    type: str
    title: str
    priority: str
    assignee_user_id: UUID | None = None
    assignee_queue_id: str | None = None
    due_at: datetime
    overdue_seconds: int = Field(
        description="Whole seconds past the due_at at the report's generated_at.",
        ge=0,
    )


class OverdueTasksReportResponse(BaseModel):
    """The wire shape for ``GET /api/v1/reports/overdue-tasks``."""

    model_config = ConfigDict(frozen=True)

    kind: str = Field(description='Report kind; always "snapshot" for the first slice.')
    generated_at: datetime
    tenant_id: UUID
    actor_id: UUID
    team: tuple[str, ...] = Field(
        description="Sorted queue keys the report covers (the actor's team).",
    )
    total: int
    items: tuple[OverdueTaskReportRow, ...]


# ---------------------------------------------------------------------------
# Custom report schemas (P12-US6, US7 / FM-053)
# ---------------------------------------------------------------------------


class FilterClauseSchema(BaseModel):
    """Wire shape for one :class:`FilterClause` (P12-US7)."""

    model_config = ConfigDict(frozen=True)

    field: str = Field(description="Stable field identifier; must be in the closed allowlist.")
    op: str = Field(description="Filter operator; must be valid for the field.")
    value: str | int | UUID | tuple[str | UUID, ...]


class CustomReportDefinitionCreate(BaseModel):
    """The body of ``POST /api/v1/reports/custom-definitions``."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1, description="Author-visible slug; unique per tenant.")
    name: str = Field(min_length=1, description="Display name; may be renamed later.")
    description: str = Field(default="", description="Free-text description.")
    group_by: tuple[str, ...] = Field(
        min_length=1,
        description="Closed-vocabulary group-by fields (e.g. ['case.status']).",
    )
    aggregations: tuple[str, ...] = Field(
        min_length=1,
        description="Closed-vocabulary aggregation kinds (e.g. ['count']).",
    )
    filters: tuple[FilterClauseSchema, ...] = Field(
        default_factory=tuple,
        description="Optional filter clauses applied before aggregation.",
    )
    time_window_hours: int | None = Field(
        default=None,
        ge=1,
        le=8760,
        description="Optional rolling time window in hours.",
    )


class CustomReportDefinitionResponse(BaseModel):
    """The wire shape for the custom-definition create response."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    key: str
    name: str
    description: str
    group_by: tuple[str, ...]
    aggregations: tuple[str, ...]
    filters: tuple[FilterClauseSchema, ...]
    time_window_hours: int | None
    created_by: UUID
    created_at: datetime
    version_token: int


class CustomReportBucketResponse(BaseModel):
    """One row of a rendered custom-report result (P12-US7)."""

    model_config = ConfigDict(frozen=True)

    key: tuple[str, ...]
    count: int
    overdue_seconds: int


class CustomReportResultResponse(BaseModel):
    """The wire shape for ``GET /api/v1/reports/custom/{definition_id}``."""

    model_config = ConfigDict(frozen=True)

    kind: str
    generated_at: datetime
    tenant_id: UUID
    actor_id: UUID
    definition_id: UUID
    definition_key: str
    definition_name: str
    total: int
    items: tuple[CustomReportBucketResponse, ...]


class ReportExportResponse(BaseModel):
    """The wire shape for ``POST /api/v1/reports/custom/{definition_id}/export``.

    ``audit_event_id`` is the id of the ``AuditExported`` row the
    use case staged so the caller can correlate the export with the
    audit chain.
    """

    model_config = ConfigDict(frozen=True)

    audit_event_id: UUID
    generated_at: datetime
    definition_id: UUID
    row_count: int
    csv_body: str


# ---------------------------------------------------------------------------
# Manager dashboard schema (P12-US8 / FM-054)
# ---------------------------------------------------------------------------


class OperationalKPIsResponse(BaseModel):
    """The KPI payload for ``GET /api/v1/dashboards/operational``.

    ``open_by_priority`` breaks ``total_open_cases`` down by priority so
    the manager can see how much of the load is urgent vs normal.
    Unknown priority values are bucketed under ``"other"`` rather than
    dropped so a future priority value never silently changes the
    total.
    """

    model_config = ConfigDict(frozen=True)

    total_open_cases: int = Field(ge=0)
    total_overdue_tasks: int = Field(ge=0)
    open_by_priority: dict[str, int] = Field(default_factory=dict)


class OperationalDashboardResponse(BaseModel):
    """The wire shape for ``GET /api/v1/reports/dashboards/operational``.

    ``generated_at`` is the system-of-record-as-of timestamp the two
    snapshot reads were issued against (P12 §1). ``freshness_seconds``
    is the gap between ``generated_at`` and ``served_at``; FM-054
    promises sub-five-second freshness so the field is capped at 5.
    ``kind`` is always ``"snapshot"`` — the dashboard is a
    point-in-time view, not an aggregate over a window.
    """

    model_config = ConfigDict(frozen=True)

    kind: str = Field(description='Dashboard kind; always "snapshot" for the first slice.')
    generated_at: datetime
    served_at: datetime
    freshness_seconds: int = Field(
        ge=0,
        le=5,
        description="Wall-clock gap between generated_at and served_at (FM-054).",
    )
    tenant_id: UUID
    actor_id: UUID
    team: tuple[str, ...] = Field(
        description="Sorted queue keys the dashboard covers (the actor's team).",
    )
    kpis: OperationalKPIsResponse


__all__ = [
    "OpenCaseReportRow",
    "OpenCasesReportResponse",
    "OverdueTaskReportRow",
    "OverdueTasksReportResponse",
    "CustomReportDefinitionCreate",
    "CustomReportDefinitionResponse",
    "CustomReportResultResponse",
    "CustomReportBucketResponse",
    "ReportExportResponse",
    "OperationalDashboardResponse",
    "OperationalKPIsResponse",
]
