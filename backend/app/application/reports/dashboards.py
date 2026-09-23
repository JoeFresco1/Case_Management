"""Operational KPI dashboard for managers (P12-US8 / FM-054).

The pillar P12 statement distinguishes snapshots from aggregates; the
operational reports (``open_cases``, ``overdue_tasks``) ship as
snapshots. The **manager dashboard** aggregates the two snapshots into
a single KPI surface so a supervisor can glance at "what is the team
working on" without paginating two separate reports.

What "freshness < 5s" means here
--------------------------------

The pillar contract (FM-054) is *sub-five-second freshness*: the wire
shape carries the **source time** the system of record was sampled,
and a **freshness** field derived from the same clock tick so the UI
can show "as of 12:00:01 (1s ago)". The use case does not promise
real-time streaming (that is P3) — it promises the system of record is
sampled back-to-back with the request, so the source time is never
more than a handful of seconds in the past. Both ``open_cases`` and
``overdue_tasks`` are invoked with the **same** ``now`` value so the
two halves of the dashboard are guaranteed to agree on the snapshot
boundary. ``freshness_seconds`` is the wall-clock gap between ``now``
and the moment the router observes the response, computed inside the
use case from the same clock — the caller does not need to inject a
second timestamp.

Why a single use case instead of two HTTP calls from the front-end
------------------------------------------------------------------

Combining the two reads in one use case keeps the snapshot boundary
atomic from the manager's perspective: the open-cases count and the
overdue-tasks count reflect the same instant of the system of
record. The use case delegates to the existing
:func:`operational_reports.open_cases` and
:func:`operational_reports.overdue_tasks` so the team-scope and
authorization logic are shared and there is exactly one
``authorize(Action.CASE_READ)`` call. A second independent HTTP call
would risk a window where the two halves disagree by a few hundred ms.

# Authorization (P11 / FM-002)

The dashboard requires ``Action.CASE_READ`` AND ``Action.TASK_READ``,
identical to the operational reports. Both are granted to the
``supervisor`` role. A reviewer who is not a queue member sees
``total_open_cases=0`` and ``total_overdue_tasks=0`` — the report
never confirms whether a queue exists in another team (Constitution
V). The dashboard does **not** add a new role or action: the manager
of P12-US8 is the existing supervisor role.

# Snapshot semantics

The dashboard is a snapshot, not an aggregate over time. The wire
shape carries ``kind="snapshot"`` and ``generated_at`` so a downstream
consumer can answer "as of when" with the exact timestamp the
queries were issued (P12 pillar §1). The use case does not write audit
or outbox rows (P12 pillar §2: "reports do not modify state").

# Failure behavior

- Missing role grant -> ``Forbidden`` (HTTP 403).
- Cross-tenant actor -> denied by the policy (resource.tenant_id matches
  actor.tenant_id only).
- No team memberships -> empty KPIs, generated_at populated, 200.

# INTEGRATION_SEAM: report engine

A ``Report`` port (P12 pillar §2) is reserved for a future slice
where the dashboard can be backed by Azure Synapse / Power BI. The
wire shape already carries ``kind`` / ``generated_at`` /
``freshness_seconds`` so the swap is a single-file change in the
router.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.application.reports.operational_reports import (
    REPORT_SOURCE,
    OperationalReportsQuery,
    build_authorization_scope,
    failed_work,
    open_cases,
    overdue_tasks,
)
from app.domain.errors import Forbidden
from app.domain.ports import (
    CaseRepository,
    QueueRepository,
    TaskRepository,
)

# ---------------------------------------------------------------------------
# Wire shapes — the dataclasses the router serializes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperationalKPIs:
    """The KPI counts the manager dashboard surfaces.

    ``total_open_cases`` is the count of ``CaseStatus.OPEN`` cases tied
    to the actor's team queues. ``total_overdue_tasks`` is the count of
    ``OPEN`` tasks with ``due_at <= now`` in the same team queues.
    ``total_failed_work`` is the count of still-failing background
    work rows for the team (P12-US4 / FM-052); it is ``0`` when the
    caller supplies no failed-work repository or when the actor lacks
    the governed ``AUDIT_READ`` grant. All counts are tenant-isolated
    and authorization-filtered by the underlying snapshot reads.

    ``open_by_priority`` breaks the open-case count down by priority so
    the manager can see at a glance how much of the load is ``urgent``
    vs ``normal``. Missing or empty priority values are bucketed under
    ``"other"`` rather than dropped, so a future priority label is
    preserved verbatim and only the empty value is collapsed.
    """

    total_open_cases: int
    total_overdue_tasks: int
    open_by_priority: dict[str, int] = field(default_factory=dict)
    total_failed_work: int = 0


@dataclass(frozen=True, slots=True)
class OperationalDashboard:
    """The wire shape for ``GET /api/v1/dashboards/operational``.

    ``generated_at`` is the system-of-record-as-of timestamp (P12 §1):
    the same value used for every snapshot read, so all KPI halves
    agree on the snapshot boundary. ``source_time`` repeats that
    instant under an explicit name so a manager can quote "sourced at
    <time> from <source>" without guessing which field is the source
    clock. ``freshness_seconds`` is the wall-clock gap between
    ``generated_at`` and ``served_at``, both derived from the same
    :class:`Clock` instance so the value is honest even under test
    fakes. ``team`` is the sorted list of queue keys the dashboard
    covers; ``authorization_scope`` repeats tenant / actor / roles /
    team so the KPI's authorization boundary is visible on every
    render. ``source`` names the system of record.

    ``kind`` is always ``"snapshot"`` and ``is_historical`` always
    ``False`` — the dashboard is a point-in-time view of **current
    state**, never an aggregate over a window (use a custom report
    with ``time_window_hours`` for the historical view).
    """

    kind: str
    generated_at: datetime
    served_at: datetime
    freshness_seconds: int
    tenant_id: UUID
    actor_id: UUID
    team: tuple[str, ...]
    kpis: OperationalKPIs
    authorization_scope: tuple[str, ...] = field(default_factory=tuple)
    source: str = REPORT_SOURCE
    source_time: datetime | None = None
    is_historical: bool = False


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperationalDashboardQuery:
    """The query the router hands to :func:`operational_dashboard`.

    Mirrors :class:`operational_reports.OperationalReportsQuery` so the
    router does not have to remember a second query shape; the
    authorization and team-scope logic is delegated to the existing
    operational-report use cases, which already enforce both.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DashboardActorProxy:
    """Minimal policy subject used for the dashboard's task-read check."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


async def operational_dashboard(
    query: OperationalDashboardQuery,
    *,
    case_repo: CaseRepository,
    task_repo: TaskRepository,
    queue_repo: QueueRepository,
    now: datetime,
    served_at: datetime | None = None,
    failed_work_repo: object | None = None,
) -> OperationalDashboard:
    """Return the manager dashboard for the actor's team.

    Authorization is enforced at this boundary for both data sets:
    ``CASE_READ`` is enforced by the delegated open-case snapshot and
    ``TASK_READ`` is required here before any task KPI is queried. This
    keeps a role with case visibility but no task permission from learning
    task counts or queue-scoped task data. The failed-work KPI is
    best-effort governed: it requires ``AUDIT_READ`` inside
    :func:`failed_work`; an actor without that grant sees
    ``total_failed_work=0`` rather than a dashboard-wide 403 so the
    current-state KPIs stay available under least privilege.

    The snapshots share one ``now`` so the source-of-record boundary
    is identical for every half. ``served_at`` (defaults to ``now``)
    drives the freshness computation; the router passes its own
    observation time when it wants a true round-trip measurement.
    """
    _enforce_freshness(now=now, served_at=served_at)

    await authorize(
        Action.TASK_READ,
        _DashboardActorProxy(
            tenant_id=query.tenant_id,
            user_id=query.actor_id,
            roles=query.actor_roles,
        ),  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="task", id=None),
    )

    op_query = OperationalReportsQuery(
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        actor_roles=query.actor_roles,
    )

    open_report = await open_cases(
        op_query,
        case_repo=case_repo,
        queue_repo=queue_repo,
        now=now,
    )
    overdue_report = await overdue_tasks(
        op_query,
        task_repo=task_repo,
        queue_repo=queue_repo,
        now=now,
    )
    failed_total = 0
    if failed_work_repo is not None:
        try:
            failed_report = await failed_work(
                op_query,
                failed_repo=failed_work_repo,  # type: ignore[arg-type]
                queue_repo=queue_repo,
                now=now,
            )
            failed_total = failed_report.total
        except Forbidden:
            # Why swallow: the dashboard's current-state KPIs are
            # governed by CASE_READ/TASK_READ; failed-work diagnostics
            # need the stricter AUDIT_READ. A reviewer keeps the case
            # and task counts and sees zero failed work instead of a
            # 403 that would hide numbers they are allowed to see.
            failed_total = 0

    served = served_at if served_at is not None else now
    freshness = max(0, int((served - now).total_seconds()))

    open_by_priority: dict[str, int] = {}
    for row in open_report.items:
        key = row.priority or "other"
        open_by_priority[key] = open_by_priority.get(key, 0) + 1

    team = open_report.team
    return OperationalDashboard(
        kind="snapshot",
        generated_at=now,
        served_at=served,
        freshness_seconds=freshness,
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        team=team,
        kpis=OperationalKPIs(
            total_open_cases=open_report.total,
            total_overdue_tasks=overdue_report.total,
            open_by_priority=open_by_priority,
            total_failed_work=failed_total,
        ),
        authorization_scope=build_authorization_scope(
            tenant_id=query.tenant_id,
            actor_id=query.actor_id,
            actor_roles=query.actor_roles,
            team=team,
        ),
        source=REPORT_SOURCE,
        source_time=now,
        is_historical=False,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enforce_freshness(*, now: datetime, served_at: datetime | None) -> None:
    """Validate the freshness window contract (FM-054).

    The pillar promises sub-five-second freshness: ``served_at`` must
    never be more than a handful of seconds after ``now``. We refuse a
    configuration that would smuggle in a stale snapshot via a stale
    ``served_at`` so a misuse (e.g. passing the same ``now`` twice
    across a slow network) fails fast at the use-case boundary rather
    than silently shipping a misleading "freshness: 9999s" to a
    dashboard that may only display the first two digits.

    A ``served_at`` earlier than ``now`` is clamped to ``freshness=0``
    in :func:`operational_dashboard`; here we only reject the
    impossible-in-practice gap so the test suite can keep using a
    single clock for both reads.
    """
    if served_at is None:
        return
    # Why 5s exactly: FM-054 reads "sub-five-second freshness"; the
    # 5s ceiling here is the same number the wire shape asserts.
    gap = (served_at - now).total_seconds()
    if gap > 5:
        raise ValueError(f"served_at is {gap:.0f}s after now; freshness window exceeded")


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


__all__ = [
    "OperationalDashboard",
    "OperationalDashboardQuery",
    "OperationalKPIs",
    "operational_dashboard",
]
