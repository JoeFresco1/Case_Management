"""Use case: render a custom report against the operational read models (P12-US7 / FM-053).

The renderer delegates to the existing operational reports use case for
the case row source so a custom report never bypasses the team-scope /
tenant-isolation contract from FM-051. Filtering / grouping /
aggregation are the closed-vocabulary operations validated at definition
time. Split out of the former single-file ``custom_reports.py`` during
the R2 package split.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from app.application.reports.operational_reports import (
    REPORT_SOURCE,
    OperationalReportsQuery,
    ReportKind,
    build_authorization_scope,
    open_cases,
)
from app.domain.case import Case
from app.domain.ports import CaseRepository, QueueRepository

from .shapes import AggregateBucket, CustomReportResult, FilterClause, ReportDefinition
from .vocabulary import AggregationKind, FilterOperator, GroupByField


async def render_custom_report(
    query: OperationalReportsQuery,
    definition: ReportDefinition,
    *,
    case_repo: CaseRepository,
    queue_repo: QueueRepository,
    now: datetime,
) -> CustomReportResult:
    """Render a custom report definition against the operational read models.

    The renderer delegates to the existing operational reports use
    case for the case row source so a custom report never bypasses
    the team-scope / tenant-isolation contract from FM-051. The
    filtering / grouping / aggregation are the closed-vocabulary
    operations validated at definition time.

    Authorization mirrors the operational reports (``CASE_READ`` +
    queue-list ``CASE_READ``). The actor must be a queue member for
    the team-scope to produce rows; an actor with no queue
    memberships sees an empty result set rather than a 403 (the
    operational reports contract). Governed export is enforced
    downstream by :func:`~.export.export_custom_report`
    (``AUDIT_EXPORT``).

    The ``kind`` marks current vs historical explicitly: no time
    window → ``SNAPSHOT`` (current state, ``is_historical=False``);
    a window → ``AGGREGATE`` (historical slice over
    ``[window_start, window_end]``, ``is_historical=True``) so a
    consumer never mistakes "open now" for "updated last week".
    """
    window_start = _validate_time_window(definition.time_window_hours, now=now)

    cases, team = await _fetch_cases(
        query=query,
        definition=definition,
        case_repo=case_repo,
        queue_repo=queue_repo,
        now=now,
    )
    buckets = _aggregate(cases, definition)
    is_historical = definition.time_window_hours is not None
    kind = ReportKind.AGGREGATE if is_historical else ReportKind.SNAPSHOT
    return CustomReportResult(
        kind=kind,
        generated_at=now,
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        definition_id=definition.id,
        definition_key=definition.key,
        definition_name=definition.name,
        total=len(buckets),
        items=tuple(buckets),
        authorization_scope=build_authorization_scope(
            tenant_id=query.tenant_id,
            actor_id=query.actor_id,
            actor_roles=query.actor_roles,
            team=team,
        ),
        source=REPORT_SOURCE,
        is_historical=is_historical,
        window_start=window_start,
        window_end=now if is_historical else None,
    )


def _validate_time_window(hours: int | None, *, now: datetime) -> datetime | None:
    """Return the ``since`` timestamp for the time window.

    Returns ``None`` when no window is configured. The window is
    applied to ``updated_at`` so a case updated outside the window
    is excluded — the same semantics as the operational reports'
    snapshot timestamp.
    """
    if hours is None:
        return None
    return now - timedelta(hours=hours)


async def _fetch_cases(
    *,
    query: OperationalReportsQuery,
    definition: ReportDefinition,
    case_repo: CaseRepository,
    queue_repo: QueueRepository,
    now: datetime,
) -> tuple[Sequence[Case], tuple[str, ...]]:
    """Resolve the scoped, filtered case set for the custom report.

    Reuses :func:`operational_reports.open_cases` for the team scope
    so a custom report cannot widen the team's queue set. The
    time-window and filter clauses are applied in-process; the case
    repository is the live source. Returns the filtered cases plus
    the resolved team so the caller can stamp ``authorization_scope``
    without a second queue read.
    """
    base_report = await open_cases(
        query,
        case_repo=case_repo,
        queue_repo=queue_repo,
        now=now,
        limit=200,
    )
    raw = await case_repo.list_open_for_queues(
        query.tenant_id,
        queue_keys=tuple(base_report.team),
        limit=200,
    )
    filtered = [c for c in raw if _matches_definition(c, definition, now=now)]
    return filtered, base_report.team


def _matches_definition(
    case: Case,
    definition: ReportDefinition,
    *,
    now: datetime,
) -> bool:
    """Apply the definition's filter clauses and time window."""
    since = _validate_time_window(definition.time_window_hours, now=now)
    if since is not None and case.updated_at < since:
        return False
    if case.status is not None:
        status_value = case.status.value if hasattr(case.status, "value") else str(case.status)
    else:
        status_value = ""
    for clause in definition.filters:
        actual: Any
        if clause.field == "case.status":
            actual = status_value
        elif clause.field == "case.priority":
            actual = case.priority
        elif clause.field == "case.queue":
            actual = getattr(case, "owner_queue_id", None) or ""
        else:
            # Filters targeting tasks are evaluated against the
            # aggregate's exposed fields; for v1 the case workspace
            # is the source and we skip the clause (an empty
            # owner_queue_id would never match an assignee filter).
            continue
        if not _match_clause(actual, clause):
            return False
    return True


def _match_clause(actual: Any, clause: FilterClause) -> bool:
    """Evaluate one :class:`~.shapes.FilterClause` against the case-side value."""
    if clause.op is FilterOperator.EQ:
        return _scalar_eq(actual, clause.value)
    if clause.op is FilterOperator.NE:
        return not _scalar_eq(actual, clause.value)
    if clause.op is FilterOperator.IN:
        values = cast(tuple[str | UUID, ...], clause.value)
        return any(_scalar_eq(actual, v) for v in values)
    # Ordering operators are not in use for v1 string fields; the
    # validator already enforces that ``gt/gte/lt/lte`` only target
    # ``task.due_at``, which is the timestamp path. The custom
    # renderer does not currently surface ``task.due_at`` on the
    # Case aggregate; the clause is conservatively rejected so a
    # future slice can add the task-side filter without changing
    # the closed vocabulary.
    return False


def _scalar_eq(actual: Any, expected: Any) -> bool:
    """Equality that tolerates string / UUID mismatches."""
    if isinstance(expected, UUID):
        if isinstance(actual, UUID):
            return actual == expected
        if isinstance(actual, str):
            try:
                return UUID(actual) == expected
            except ValueError:
                return False
        return False
    if isinstance(expected, str):
        return str(actual) == expected
    return bool(actual == expected)


def _aggregate(
    cases: Iterable[Case],
    definition: ReportDefinition,
) -> list[AggregateBucket]:
    """Group + aggregate the filtered cases against the definition.

    ``COUNT`` is computed against the case set; ``OVERDUE_SUM`` reads
    the optional ``overdue_seconds`` field on the case aggregate when
    present (the operational reports carry it on the task side; the
    case workspace will populate it when SLA clocks land). The
    renderer is permissive: an ``OVERDUE_SUM`` aggregation in the
    absence of a value yields ``0`` so the bucket is still rendered.
    """
    groups: dict[tuple[str, ...], list[Case]] = {}
    for case in cases:
        key = tuple(_project_value(case, gb) for gb in definition.group_by)
        groups.setdefault(key, []).append(case)
    buckets: list[AggregateBucket] = []
    for key, rows in groups.items():
        overdue_sum = 0
        if AggregationKind.OVERDUE_SUM in definition.aggregations:
            for case in rows:
                value = getattr(case, "overdue_seconds", 0) or 0
                overdue_sum += int(value)
        buckets.append(AggregateBucket(key=key, count=len(rows), overdue_seconds=overdue_sum))
    buckets.sort(key=lambda b: b.key)
    return buckets


_GROUP_BY_PROJECTORS: dict[GroupByField, Any] = {
    GroupByField.CASE_STATUS: lambda case: (
        case.status.value if hasattr(case.status, "value") else str(case.status)
    ),
    GroupByField.CASE_PRIORITY: lambda case: case.priority,
    GroupByField.QUEUE_KEY: lambda case: str(getattr(case, "owner_queue_id", "") or ""),
    GroupByField.WORKFLOW_STATE: lambda case: str(case.current_workflow_state or ""),
    GroupByField.ASSIGNEE_USER: lambda _case: "",
    GroupByField.ASSIGNEE_QUEUE: lambda _case: "",
}


def _project_value(case: Case, field: GroupByField) -> str:
    """Project one grouped field value onto a wire string."""
    projector = _GROUP_BY_PROJECTORS.get(field)
    if projector is None:
        return ""
    return str(projector(case))


__all__ = [
    "render_custom_report",
    "_GROUP_BY_PROJECTORS",
    "_aggregate",
    "_fetch_cases",
    "_match_clause",
    "_matches_definition",
    "_project_value",
    "_scalar_eq",
    "_validate_time_window",
]
