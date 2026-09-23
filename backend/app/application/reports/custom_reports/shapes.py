"""Wire shapes — the dataclasses the router serializes (P12-US6, US7 / FM-053).

Frozen value objects passed between the HTTP layer and the custom-report
use cases: the declarative :class:`ReportDefinition` spec, the define /
export commands, the rendered result rows, and the duck-typed actor
proxy the authorization policy consumes. Split out of the former
single-file ``custom_reports.py`` during the R2 package split; no
behavior lives here beyond :class:`ReportDefinition`'s author-time
invariant checks in ``__post_init__``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.application.reports.operational_reports import REPORT_SOURCE, ReportKind
from app.domain.errors import ValidationFailed

from .vocabulary import AggregationKind, FilterOperator, GroupByField


@dataclass(frozen=True, slots=True)
class FilterClause:
    """A single leaf of the filter expression.

    ``field`` is a stable identifier from
    :data:`~.vocabulary._FILTERABLE_FIELDS`.
    ``op`` is a :class:`~.vocabulary.FilterOperator` accepted for that
    field. ``value`` is the comparison value; for ``IN`` it must be a
    tuple of strings / UUIDs the adapter can project. The validator
    normalizes the value into a tuple so a future SQLite / Postgres
    adapter can emit the same parameter binding shape.
    """

    field: str
    op: FilterOperator
    value: str | int | UUID | tuple[str | UUID, ...]


@dataclass(frozen=True, slots=True)
class ReportDefinition:
    """A controlled declarative custom-report spec (P12-US7 / FM-053).

    The definition is tenant-scoped. ``key`` is the author-visible
    identifier (a slug the UI binds to); ``name`` is a free-text label
    that may be renamed without breaking references. The
    ``group_by`` / ``aggregations`` / ``filters`` / ``time_window``
    fields carry the closed vocabulary described in :mod:`.vocabulary`.

    ``version_token`` is bumped on every successful redefinition so a
    future optimistic-concurrency contract on the definition row is a
    one-line add. ``created_by`` / ``created_at`` record the
    publication provenance for the audit timeline.
    """

    id: UUID
    tenant_id: UUID
    key: str
    name: str
    description: str
    group_by: tuple[GroupByField, ...]
    aggregations: tuple[AggregationKind, ...]
    filters: tuple[FilterClause, ...]
    time_window_hours: int | None
    created_by: UUID
    created_at: datetime
    version_token: int = 1

    def __post_init__(self) -> None:
        # Author-time invariants: the closed-vocabulary check is also
        # performed by :func:`~.define.define_custom_report`, but repeating the
        # assert here means a future caller cannot construct an
        # invalid aggregate through the dataclass path.
        if not self.key:
            raise ValidationFailed("definition.key is required", field="key")
        if not self.name:
            raise ValidationFailed("definition.name is required", field="name")
        if not self.group_by:
            raise ValidationFailed(
                "definition.group_by must contain at least one field",
                field="group_by",
            )
        if not self.aggregations:
            raise ValidationFailed(
                "definition.aggregations must contain at least one kind",
                field="aggregations",
            )


@dataclass(frozen=True, slots=True)
class DefineCustomReportCommand:
    """The input to :func:`~.define.define_custom_report`.

    Mirrors :class:`ReportDefinition` minus the server-assigned id /
    timestamps / version_token. The use case is the only place those
    are produced so the audit chain always agrees with the persisted
    row.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    key: str
    name: str
    description: str
    group_by: tuple[GroupByField, ...]
    aggregations: tuple[AggregationKind, ...]
    filters: tuple[FilterClause, ...]
    time_window_hours: int | None


@dataclass(frozen=True, slots=True)
class AggregateBucket:
    """A single row in the rendered custom-report result (P12-US7 / FM-053).

    ``key`` is the tuple of grouped field values; ``count`` is the
    number of cases / tasks in the bucket; ``overdue_seconds`` is the
    sum of overdue windows for tasks in the bucket (zero when the
    bucket is cases or when no task is overdue in the window). The
    CSV export renders these two fields today; future aggregation
    kinds (``MEDIAN``, ``P95``) extend this shape without breaking
    the wire contract.
    """

    key: tuple[str, ...]
    count: int
    overdue_seconds: int


@dataclass(frozen=True, slots=True)
class CustomReportResult:
    """The result of rendering a :class:`ReportDefinition`.

    ``generated_at`` is the source timestamp (P12 §1) — the instant
    the system of record was sampled. ``kind`` distinguishes **current
    state** (``SNAPSHOT`` when the definition sets no time window)
    from **historical** (``AGGREGATE`` when ``time_window_hours`` is
    set): a snapshot answers "what is open now", an aggregate answers
    "what updated over the last N hours". ``is_historical`` repeats
    that distinction as a boolean so the UI can badge the two without
    string-matching. ``window_start`` / ``window_end`` bound the
    historical slice (``None`` for snapshots).
    ``authorization_scope`` repeats tenant / actor / roles / team so
    the grouping is auditable, and ``source`` names the system of
    record. ``definition_id`` is the resolved definition id.
    ``items`` is the bucketed aggregation. ``total`` is the count of
    buckets so the UI can render "N groups" without scanning the list.
    """

    kind: ReportKind
    generated_at: datetime
    tenant_id: UUID
    actor_id: UUID
    definition_id: UUID
    definition_key: str
    definition_name: str
    total: int
    items: tuple[AggregateBucket, ...] = field(default_factory=tuple)
    authorization_scope: tuple[str, ...] = field(default_factory=tuple)
    source: str = REPORT_SOURCE
    is_historical: bool = False
    window_start: datetime | None = None
    window_end: datetime | None = None


@dataclass(frozen=True, slots=True)
class ExportResult:
    """The output of :func:`~.export.export_custom_report`.

    The CSV body is rendered by the **router** from the items list so
    the application layer never owns the wire format. ``audit_row_id``
    is a synthetic identifier for the staged ``AuditExported`` row;
    the production adapter computes it from the row's payload hash
    (same idempotency key) so the caller can correlate the export
    with the audit chain without depending on the persistence layer
    returning a generated id.
    """

    report: CustomReportResult
    csv_body: str
    audit_row_id: UUID


__all__ = [
    "AggregateBucket",
    "CustomReportResult",
    "DefineCustomReportCommand",
    "ExportResult",
    "FilterClause",
    "ReportDefinition",
]
