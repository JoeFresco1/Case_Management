"""Use cases: open-cases, overdue-tasks, and failed-work snapshots (P12).

Split out of the former single-file ``operational_reports.py`` during the
R2 package split. Each use case resolves the actor's live team scope
through :func:`~.helpers._resolve_team`, enforces deny-by-default
authorization, and returns a frozen snapshot envelope with
``generated_at`` / ``authorization_scope`` / ``source`` populated so the
wire can answer "as of when, sourced from where, under which grant".

The use cases are pure: no FastAPI / SQLAlchemy imports — the router is
the only place that wires the SQL repositories through the dependency
graph. Reports are read-only; no audit or outbox rows are written here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.application.authorization import Action, Resource, authorize
from app.domain.errors import ValidationFailed
from app.domain.ports import (
    CaseRepository,
    QueueRepository,
    TaskRepository,
)

from .helpers import (
    _ActorProxy,
    _case_to_row,
    _enforce_limit,
    _failed_to_row,
    _resolve_team,
    _task_to_row,
    build_authorization_scope,
)
from .shapes import (
    FailedWorkReport,
    FailedWorkRepository,
    FailedWorkRow,
    OpenCasesReport,
    OperationalReportsQuery,
    OverdueTasksReport,
)
from .vocabulary import REPORT_SOURCE, ReportKind


async def open_cases(
    query: OperationalReportsQuery,
    *,
    case_repo: CaseRepository,
    queue_repo: QueueRepository,
    now: datetime,
    limit: int = 200,
) -> OpenCasesReport:
    """Return the snapshot of open cases for the actor's team.

    Authorization:
    - ``Action.CASE_READ`` must be granted to the actor (P11).
    - The actor must be a member of at least one queue in the
      tenant. The team scope is the membership list, not the queue
      configuration: a queue the supervisor does not belong to is
      invisible to this report.

    Ordering: most-recently-updated first so the supervisor sees the
    newest work at the top of the list. ``limit`` clamps the result
    set to the HTTP page size; the total count above the page is
    computed against the un-truncated scope so the wire reports the
    true size (the supervisor can paginate by adjusting filters later).
    """
    _enforce_limit(limit)
    team_queues = await _resolve_team(
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        actor_roles=query.actor_roles,
        queue_repo=queue_repo,
    )
    if not team_queues:
        return OpenCasesReport(
            kind=ReportKind.SNAPSHOT,
            generated_at=now,
            tenant_id=query.tenant_id,
            actor_id=query.actor_id,
            team=(),
            total=0,
            items=(),
            authorization_scope=build_authorization_scope(
                tenant_id=query.tenant_id,
                actor_id=query.actor_id,
                actor_roles=query.actor_roles,
                team=(),
            ),
            source=REPORT_SOURCE,
            is_historical=False,
        )

    raw_cases = await case_repo.list_open_for_queues(
        query.tenant_id,
        queue_keys=tuple(team_queues),
        limit=limit,
    )
    items = tuple(_case_to_row(c) for c in raw_cases)
    team = tuple(sorted(team_queues))
    return OpenCasesReport(
        kind=ReportKind.SNAPSHOT,
        generated_at=now,
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        team=team,
        total=len(items),
        items=items,
        authorization_scope=build_authorization_scope(
            tenant_id=query.tenant_id,
            actor_id=query.actor_id,
            actor_roles=query.actor_roles,
            team=team,
        ),
        source=REPORT_SOURCE,
        is_historical=False,
    )


async def overdue_tasks(
    query: OperationalReportsQuery,
    *,
    task_repo: TaskRepository,
    queue_repo: QueueRepository,
    now: datetime,
    limit: int = 200,
) -> OverdueTasksReport:
    """Return the snapshot of overdue tasks for the actor's team.

    Overdue is ``status == OPEN`` AND ``due_at <= now``. Tasks without
    a ``due_at`` are never overdue (a task with no SLA window is by
    definition not yet past its deadline).
    """
    _enforce_limit(limit)
    team_queues = await _resolve_team(
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        actor_roles=query.actor_roles,
        queue_repo=queue_repo,
    )
    if not team_queues:
        return OverdueTasksReport(
            kind=ReportKind.SNAPSHOT,
            generated_at=now,
            tenant_id=query.tenant_id,
            actor_id=query.actor_id,
            team=(),
            total=0,
            items=(),
            authorization_scope=build_authorization_scope(
                tenant_id=query.tenant_id,
                actor_id=query.actor_id,
                actor_roles=query.actor_roles,
                team=(),
            ),
            source=REPORT_SOURCE,
            is_historical=False,
        )

    raw_tasks = await task_repo.list_overdue_for_queues(
        query.tenant_id,
        queue_keys=tuple(team_queues),
        now=now,
        limit=limit,
    )
    items = tuple(_task_to_row(t, now=now) for t in raw_tasks)
    team = tuple(sorted(team_queues))
    return OverdueTasksReport(
        kind=ReportKind.SNAPSHOT,
        generated_at=now,
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        team=team,
        total=len(items),
        items=items,
        authorization_scope=build_authorization_scope(
            tenant_id=query.tenant_id,
            actor_id=query.actor_id,
            actor_roles=query.actor_roles,
            team=team,
        ),
        source=REPORT_SOURCE,
        is_historical=False,
    )


async def failed_work(
    query: OperationalReportsQuery,
    *,
    failed_repo: FailedWorkRepository,
    queue_repo: QueueRepository,
    now: datetime,
    limit: int = 200,
    window_hours: int | None = None,
) -> FailedWorkReport:
    """Return the snapshot of still-failing background work for the team.

    Authorization (governed, P12-US4 / FM-052):

    - ``Action.CASE_READ`` via :func:`~.helpers._resolve_team` (team
      membership).
    - ``Action.AUDIT_READ`` additionally, because failed work carries
      operator diagnostics. A reviewer holds ``CASE_READ`` but not
      ``AUDIT_READ`` and is denied with ``Forbidden`` (never an empty
      leak that confirms queue existence differently per role).

    Team scope: only rows whose ``queue_key`` is in the actor's team
    (or queue-unbound rows when the actor has any team) are returned;
    the repository is tenant-scoped so cross-tenant rows are invisible.
    An actor with no team sees an empty snapshot, not a 404.

    Staleness: when ``window_hours`` is set, rows whose
    ``last_attempt_at`` predates ``now - window`` are excluded as
    stale — the operator triages what is failing *now*, not last
    quarter. The snapshot stays ``is_historical=False``; a windowed
    custom aggregate is the historical view (FM-053).
    """
    _enforce_limit(limit)
    if window_hours is not None and (window_hours <= 0 or window_hours > 24 * 365):
        raise ValidationFailed(
            "window_hours must be in [1, 8760] when set",
            field="window_hours",
        )
    team_queues = await _resolve_team(
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        actor_roles=query.actor_roles,
        queue_repo=queue_repo,
    )
    # Why a second authorize after _resolve_team: the team check proves
    # queue visibility, but failed-work diagnostics are operator-grade.
    # Supervisors hold AUDIT_READ; reviewers do not, so a reviewer who
    # passes the team gate is still denied here (governed export rule).
    await authorize(
        Action.AUDIT_READ,
        _ActorProxy(
            tenant_id=query.tenant_id,
            user_id=query.actor_id,
            roles=query.actor_roles,
        ),  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="failed_work", id=None),
    )
    if not team_queues:
        return FailedWorkReport(
            kind=ReportKind.SNAPSHOT,
            generated_at=now,
            tenant_id=query.tenant_id,
            actor_id=query.actor_id,
            team=(),
            total=0,
            items=(),
            authorization_scope=build_authorization_scope(
                tenant_id=query.tenant_id,
                actor_id=query.actor_id,
                actor_roles=query.actor_roles,
                team=(),
            ),
            source=REPORT_SOURCE,
            is_historical=False,
        )

    team_set = set(team_queues)
    raw = await failed_repo.list_failed(
        query.tenant_id,
        queue_keys=tuple(team_queues),
        limit=limit,
    )
    cutoff: datetime | None = None
    if window_hours is not None:
        cutoff = now - timedelta(hours=window_hours)
    rows: list[FailedWorkRow] = []
    for item in raw:
        row = _failed_to_row(item)
        # Why team-filter here as well: the repository is asked with
        # the team set, but a queue-unbound row (queue_key None) has
        # no team affinity at the SQL layer. The use case keeps such
        # rows only when the actor has a team (operator context) and
        # drops anything explicitly tagged to another team so a
        # supervisor never learns another team's poison messages.
        if row.queue_key is not None and row.queue_key not in team_set:
            continue
        if cutoff is not None and row.last_attempt_at < cutoff:
            continue
        rows.append(row)
    team = tuple(sorted(team_queues))
    return FailedWorkReport(
        kind=ReportKind.SNAPSHOT,
        generated_at=now,
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        team=team,
        total=len(rows),
        items=tuple(rows),
        authorization_scope=build_authorization_scope(
            tenant_id=query.tenant_id,
            actor_id=query.actor_id,
            actor_roles=query.actor_roles,
            team=team,
        ),
        source=REPORT_SOURCE,
        is_historical=False,
    )


__all__ = ["failed_work", "open_cases", "overdue_tasks"]
