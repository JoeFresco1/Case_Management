"""Reporting depth: failed-work, current-vs-historical, KPI source/scope (P12 / FM-051/053/054).

Covers the acceptance for the reporting extension built on F-1/F-4:

- Open-case / overdue-task snapshots carry source time, source marker,
  authorization scope, and ``is_historical=False`` (current state).
- Failed-work snapshot is team-scoped and governed (``AUDIT_READ``);
  reviewers are denied, cross-tenant rows are invisible, stale rows
  outside the window are excluded, no-team actors see empty (not 404).
- Custom reports distinguish current (SNAPSHOT, no window) from
  historical (AGGREGATE, windowed) with explicit window bounds, closed
  grouping/aggregation, and authorization scope.
- KPI dashboard shows source time + authorization scope, includes the
  failed-work KPI, enforces team boundaries, and rejects stale
  ``served_at`` beyond the 5s freshness budget.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md §4.R5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.reports import custom_reports, dashboards, operational_reports
from app.deps import UserContext
from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.errors import Forbidden
from app.domain.queue import AssignmentPolicy, Queue

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
PAST_DUE = NOW - timedelta(hours=2)
STALE_ATTEMPT = NOW - timedelta(days=10)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    id: UUID
    tenant_id: UUID
    case_id: UUID
    type: str
    title: str
    status: str = "open"
    priority: str = "normal"
    assignee_user_id: UUID | None = None
    assignee_queue_id: str | None = None
    due_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: NOW)
    version_token: int = 1


class InMemoryTaskRepository:
    def __init__(self) -> None:
        self._tasks: dict[tuple[UUID, UUID], FakeTask] = {}

    def seed(self, task: FakeTask) -> FakeTask:
        self._tasks[(task.tenant_id, task.id)] = task
        return task

    async def list_overdue_for_queues(
        self,
        tenant_id: UUID,
        *,
        queue_keys: tuple[str, ...],
        now: datetime,
        limit: int = 200,
    ) -> list[FakeTask]:
        keys = set(queue_keys)
        return sorted(
            (
                t
                for (tid, _), t in self._tasks.items()
                if tid == tenant_id
                and t.status == "open"
                and t.due_at is not None
                and t.due_at <= now
                and t.assignee_queue_id in keys
            ),
            key=lambda t: t.due_at or NOW,
        )[:limit]


class InMemoryCaseRepository:
    def __init__(self) -> None:
        self._cases: dict[tuple[UUID, UUID], Case] = {}

    def seed(self, case: Case) -> None:
        self._cases[(case.tenant_id, case.id)] = case

    async def list_open_for_queues(
        self,
        tenant_id: UUID,
        *,
        queue_keys: tuple[str, ...],
        limit: int = 200,
    ) -> list[Case]:
        wanted = set(getattr(self, "_open_case_ids", ()))
        return [
            c
            for (tid, _), c in self._cases.items()
            if tid == tenant_id and c.status is CaseStatus.OPEN and c.id in wanted
        ][:limit]


class InMemoryQueueRepository:
    def __init__(self) -> None:
        self._queues: dict[tuple[UUID, UUID], Queue] = {}

    def seed(self, queue: Queue) -> None:
        self._queues[(queue.tenant_id, queue.id)] = queue

    async def list_queues(self, tenant_id: UUID) -> list[Queue]:
        return sorted(
            (q for (tid, _), q in self._queues.items() if tid == tenant_id),
            key=lambda q: q.key,
        )


class InMemoryFailedRepository:
    """Fake FailedWorkRepository: tenant-scoped, team-filtered by the use case."""

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, UUID], operational_reports.FailedWorkRow] = {}

    def seed(self, row: operational_reports.FailedWorkRow) -> None:
        # Why tenant from queue affinity is insufficient: rows carry no
        # tenant field, so the fake keys on the caller-supplied tenant.
        # The use case always passes the actor tenant, mirroring the SQL
        # ``WHERE tenant_id = :tenant_id`` contract.
        self._rows[(uuid4(), row.work_id)] = row

    def seed_for(self, tenant_id: UUID, row: operational_reports.FailedWorkRow) -> None:
        self._rows[(tenant_id, row.work_id)] = row

    async def list_failed(
        self,
        tenant_id: UUID,
        *,
        queue_keys: tuple[str, ...],  # noqa: ARG002
        limit: int = 200,
    ) -> list[Any]:
        return [r for (tid, _), r in self._rows.items() if tid == tenant_id][:limit]


def _supervisor(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(), tenant_id=tenant_id, display_name="supervisor", roles=("supervisor",)
    )


def _reviewer(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(), tenant_id=tenant_id, display_name="reviewer", roles=("reviewer",)
    )


def _process_admin(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant_id,
        display_name="admin",
        roles=("process_admin",),
    )


def _queue(tenant_id: UUID, key: str, member: UUID) -> Queue:
    return Queue.create(
        tenant_id=tenant_id,
        key=key,
        name=key,
        member_user_ids=(member,),
        assignment_policy=AssignmentPolicy.MANUAL,
    )


def _build_case(*, tenant_id: UUID, priority: str = "normal") -> Case:
    return Case(
        id=uuid4(),
        tenant_id=tenant_id,
        reference=CaseReference(f"C-ACME-{NOW.year}-{(uuid4().int % 1_000_000):06d}"),
        case_type_version_id=uuid4(),
        status=CaseStatus.OPEN,
        priority=priority,
        title="Sample case",
        description="",
        data={},
        current_workflow_state=None,
        version_token=1,
        created_at=NOW,
        updated_at=NOW,
        created_by=uuid4(),
    )


def _failed_row(
    *, queue_key: str | None, last_attempt_at: datetime = NOW
) -> operational_reports.FailedWorkRow:
    return operational_reports.FailedWorkRow(
        work_id=uuid4(),
        event_type="case.created",
        queue_key=queue_key,
        case_id=None,
        attempt_count=5,
        last_error="delivery refused",
        first_failed_at=last_attempt_at - timedelta(hours=1),
        last_attempt_at=last_attempt_at,
        replay_eligible=True,
    )


def _query(user: UserContext) -> operational_reports.OperationalReportsQuery:
    return operational_reports.OperationalReportsQuery(
        tenant_id=user.tenant_id, actor_id=user.user_id, actor_roles=user.roles
    )


def _clock(now: datetime) -> Any:
    class _Clock:
        def now(self) -> datetime:
            return now

    return _Clock()


# ---------------------------------------------------------------------------
# Current-state snapshots: source time + authorization scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_cases_snapshot__shows_source_time_and_auth_scope() -> None:
    """Open-cases snapshot is current-state with source time + auth scope."""
    supervisor = _supervisor()
    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()
    case = _build_case(tenant_id=TENANT_A)
    case_repo.seed(case)
    case_repo._open_case_ids = (case.id,)  # type: ignore[attr-defined]
    queue_repo.seed(_queue(TENANT_A, "intake_queue", supervisor.user_id))

    report = await operational_reports.open_cases(
        _query(supervisor), case_repo=case_repo, queue_repo=queue_repo, now=NOW
    )

    assert report.kind == operational_reports.ReportKind.SNAPSHOT
    assert report.is_historical is False
    assert report.generated_at == NOW
    assert report.source == operational_reports.REPORT_SOURCE
    assert report.window_start is None
    assert report.window_end is None
    assert report.total == 1
    # // then: authorization scope names tenant, actor, role, and team.
    assert f"tenant:{TENANT_A}" in report.authorization_scope
    assert f"actor:{supervisor.user_id}" in report.authorization_scope
    assert "role:supervisor" in report.authorization_scope
    assert "team:intake_queue" in report.authorization_scope


@pytest.mark.asyncio
async def test_overdue_tasks_snapshot__shows_source_time_and_auth_scope() -> None:
    """Overdue-tasks snapshot is current-state with source time + auth scope."""
    supervisor = _supervisor()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", supervisor.user_id))
    task_repo.seed(
        FakeTask(
            id=uuid4(),
            tenant_id=TENANT_A,
            case_id=uuid4(),
            type="review",
            title="Past-due",
            due_at=PAST_DUE,
            assignee_queue_id="intake_queue",
        )
    )

    report = await operational_reports.overdue_tasks(
        _query(supervisor), task_repo=task_repo, queue_repo=queue_repo, now=NOW
    )

    assert report.kind == operational_reports.ReportKind.SNAPSHOT
    assert report.is_historical is False
    assert report.generated_at == NOW
    assert report.source == operational_reports.REPORT_SOURCE
    assert report.total == 1
    assert report.items[0].overdue_seconds > 0
    assert "team:intake_queue" in report.authorization_scope


# ---------------------------------------------------------------------------
# Failed work: team boundaries + governed access + stale + cross-tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_work_team_scoped__returns_only_team_rows() -> None:
    """Supervisor sees only failed rows tagged to their own team queues."""
    supervisor = _supervisor()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", supervisor.user_id))
    queue_repo.seed(_queue(TENANT_A, "other_queue", uuid4()))
    failed_repo = InMemoryFailedRepository()
    team_row = _failed_row(queue_key="intake_queue")
    other_row = _failed_row(queue_key="other_queue")
    failed_repo.seed_for(TENANT_A, team_row)
    failed_repo.seed_for(TENANT_A, other_row)

    report = await operational_reports.failed_work(
        _query(supervisor), failed_repo=failed_repo, queue_repo=queue_repo, now=NOW
    )

    assert report.kind == operational_reports.ReportKind.SNAPSHOT
    assert report.is_historical is False
    assert report.generated_at == NOW
    assert report.team == ("intake_queue",)
    assert report.total == 1
    assert report.items[0].work_id == team_row.work_id
    assert "team:intake_queue" in report.authorization_scope


@pytest.mark.asyncio
async def test_failed_work_reviewer_without_audit_read__forbidden() -> None:
    """Reviewer holds CASE_READ but not AUDIT_READ: governed denial, no leak."""
    reviewer = _reviewer()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", reviewer.user_id))
    failed_repo = InMemoryFailedRepository()
    failed_repo.seed_for(TENANT_A, _failed_row(queue_key="intake_queue"))

    with pytest.raises(Forbidden):
        await operational_reports.failed_work(
            _query(reviewer), failed_repo=failed_repo, queue_repo=queue_repo, now=NOW
        )


@pytest.mark.asyncio
async def test_failed_work_cross_tenant__invisible() -> None:
    """Tenant-B supervisor cannot see tenant-A failed rows (empty, not 404)."""
    supervisor_b = _supervisor(tenant_id=TENANT_B)
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", uuid4()))
    failed_repo = InMemoryFailedRepository()
    failed_repo.seed_for(TENANT_A, _failed_row(queue_key="intake_queue"))

    report = await operational_reports.failed_work(
        _query(supervisor_b), failed_repo=failed_repo, queue_repo=queue_repo, now=NOW
    )

    assert report.total == 0
    assert report.items == ()
    assert report.team == ()


@pytest.mark.asyncio
async def test_failed_work_stale_window__excludes_old_rows() -> None:
    """Windowed failed-work excludes rows whose last attempt is stale."""
    supervisor = _supervisor()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", supervisor.user_id))
    failed_repo = InMemoryFailedRepository()
    fresh = _failed_row(queue_key="intake_queue", last_attempt_at=NOW)
    stale = _failed_row(queue_key="intake_queue", last_attempt_at=STALE_ATTEMPT)
    failed_repo.seed_for(TENANT_A, fresh)
    failed_repo.seed_for(TENANT_A, stale)

    report = await operational_reports.failed_work(
        _query(supervisor),
        failed_repo=failed_repo,
        queue_repo=queue_repo,
        now=NOW,
        window_hours=24,
    )

    assert report.total == 1
    assert report.items[0].work_id == fresh.work_id


@pytest.mark.asyncio
async def test_failed_work_no_team__empty_not_404() -> None:
    """Authorized supervisor with no queue memberships sees an empty snapshot."""
    supervisor = _supervisor()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", uuid4()))
    failed_repo = InMemoryFailedRepository()

    report = await operational_reports.failed_work(
        _query(supervisor), failed_repo=failed_repo, queue_repo=queue_repo, now=NOW
    )

    assert report.total == 0
    assert report.team == ()
    assert report.generated_at == NOW


# ---------------------------------------------------------------------------
# Custom reports: grouping / aggregation / window + current-vs-historical
# ---------------------------------------------------------------------------


async def _define(
    admin: UserContext,
    repo: custom_reports.ReportDefinitionRepository,
    *,
    key: str,
    window: int | None,
) -> custom_reports.ReportDefinition:
    return await custom_reports.define_custom_report(
        custom_reports.DefineCustomReportCommand(
            tenant_id=admin.tenant_id,
            actor_id=admin.user_id,
            actor_roles=admin.roles,
            key=key,
            name=key,
            description="",
            group_by=(custom_reports.GroupByField.CASE_PRIORITY,),
            aggregations=(custom_reports.AggregationKind.COUNT,),
            filters=(),
            time_window_hours=window,
        ),
        repo=repo,
        clock=_clock(NOW),
    )


@pytest.mark.asyncio
async def test_custom_report_snapshot_vs_aggregate__kind_is_clear() -> None:
    """No window -> SNAPSHOT/current; window -> AGGREGATE/historical + bounds."""
    admin = _process_admin()
    repo = custom_reports.ReportDefinitionRepository()
    snapshot_def = await _define(admin, repo, key="snap", window=None)
    aggregate_def = await _define(admin, repo, key="hist", window=24)

    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", admin.user_id))
    case_repo.seed(_build_case(tenant_id=TENANT_A))
    case_repo._open_case_ids = tuple(c.id for c in case_repo._cases.values())  # type: ignore[attr-defined]

    snapshot = await custom_reports.render_custom_report(
        _query(admin), snapshot_def, case_repo=case_repo, queue_repo=queue_repo, now=NOW
    )
    historical = await custom_reports.render_custom_report(
        _query(admin), aggregate_def, case_repo=case_repo, queue_repo=queue_repo, now=NOW
    )

    assert snapshot.kind == operational_reports.ReportKind.SNAPSHOT
    assert snapshot.is_historical is False
    assert snapshot.window_start is None
    assert snapshot.window_end is None
    assert historical.kind == operational_reports.ReportKind.AGGREGATE
    assert historical.is_historical is True
    assert historical.window_start == NOW - timedelta(hours=24)
    assert historical.window_end == NOW
    assert historical.generated_at == NOW
    assert f"tenant:{TENANT_A}" in historical.authorization_scope


@pytest.mark.asyncio
async def test_custom_report_grouping_aggregation__buckets_counts() -> None:
    """Closed grouping + COUNT aggregation buckets cases by priority."""
    admin = _process_admin()
    repo = custom_reports.ReportDefinitionRepository()
    definition = await _define(admin, repo, key="by-prio", window=None)

    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", admin.user_id))
    case_repo.seed(_build_case(tenant_id=TENANT_A, priority="high"))
    case_repo.seed(_build_case(tenant_id=TENANT_A, priority="high"))
    case_repo.seed(_build_case(tenant_id=TENANT_A, priority="normal"))
    case_repo._open_case_ids = tuple(c.id for c in case_repo._cases.values())  # type: ignore[attr-defined]

    report = await custom_reports.render_custom_report(
        _query(admin), definition, case_repo=case_repo, queue_repo=queue_repo, now=NOW
    )

    counts = {bucket.key[0]: bucket.count for bucket in report.items}
    assert counts == {"high": 2, "normal": 1}


@pytest.mark.asyncio
async def test_custom_report_cross_tenant__empty() -> None:
    """Tenant-B actor rendering a tenant-A definition sees no rows (team empty)."""
    admin_a = _process_admin(tenant_id=TENANT_A)
    repo = custom_reports.ReportDefinitionRepository()
    definition = await _define(admin_a, repo, key="xa", window=None)

    admin_b = _process_admin(tenant_id=TENANT_B)
    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", admin_a.user_id))
    case_repo.seed(_build_case(tenant_id=TENANT_A))
    case_repo._open_case_ids = tuple(c.id for c in case_repo._cases.values())  # type: ignore[attr-defined]

    report = await custom_reports.render_custom_report(
        _query(admin_b), definition, case_repo=case_repo, queue_repo=queue_repo, now=NOW
    )

    assert report.total == 0
    assert f"tenant:{TENANT_B}" in report.authorization_scope


# ---------------------------------------------------------------------------
# KPIs: source time + authorization scope + team boundaries + stale
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_kpis__show_source_time_and_auth_scope() -> None:
    """Dashboard KPIs carry source time, source marker, and auth scope."""
    supervisor = _supervisor()
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", supervisor.user_id))
    case = _build_case(tenant_id=TENANT_A, priority="urgent")
    case_repo.seed(case)
    case_repo._open_case_ids = (case.id,)  # type: ignore[attr-defined]
    task_repo.seed(
        FakeTask(
            id=uuid4(),
            tenant_id=TENANT_A,
            case_id=case.id,
            type="review",
            title="Past-due",
            due_at=PAST_DUE,
            assignee_queue_id="intake_queue",
        )
    )
    failed_repo = InMemoryFailedRepository()
    failed_repo.seed_for(TENANT_A, _failed_row(queue_key="intake_queue"))

    dashboard = await dashboards.operational_dashboard(
        dashboards.OperationalDashboardQuery(
            tenant_id=supervisor.tenant_id,
            actor_id=supervisor.user_id,
            actor_roles=supervisor.roles,
        ),
        case_repo=case_repo,
        task_repo=task_repo,
        queue_repo=queue_repo,
        now=NOW,
        failed_work_repo=failed_repo,
    )

    assert dashboard.kind == "snapshot"
    assert dashboard.is_historical is False
    assert dashboard.generated_at == NOW
    assert dashboard.source_time == NOW
    assert dashboard.source == operational_reports.REPORT_SOURCE
    assert dashboard.freshness_seconds == 0
    assert dashboard.kpis.total_open_cases == 1
    assert dashboard.kpis.total_overdue_tasks == 1
    assert dashboard.kpis.total_failed_work == 1
    assert dashboard.kpis.open_by_priority == {"urgent": 1}
    assert f"tenant:{TENANT_A}" in dashboard.authorization_scope
    assert "role:supervisor" in dashboard.authorization_scope
    assert "team:intake_queue" in dashboard.authorization_scope


@pytest.mark.asyncio
async def test_dashboard_reviewer_without_audit__failed_zero_not_forbidden() -> None:
    """Reviewer keeps case/task KPIs; governed failed-work collapses to zero."""
    reviewer = _reviewer()
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", reviewer.user_id))
    case = _build_case(tenant_id=TENANT_A)
    case_repo.seed(case)
    case_repo._open_case_ids = (case.id,)  # type: ignore[attr-defined]
    failed_repo = InMemoryFailedRepository()
    failed_repo.seed_for(TENANT_A, _failed_row(queue_key="intake_queue"))

    dashboard = await dashboards.operational_dashboard(
        dashboards.OperationalDashboardQuery(
            tenant_id=reviewer.tenant_id,
            actor_id=reviewer.user_id,
            actor_roles=reviewer.roles,
        ),
        case_repo=case_repo,
        task_repo=task_repo,
        queue_repo=queue_repo,
        now=NOW,
        failed_work_repo=failed_repo,
    )

    assert dashboard.kpis.total_open_cases == 1
    assert dashboard.kpis.total_failed_work == 0


@pytest.mark.asyncio
async def test_dashboard_cross_tenant__zeros() -> None:
    """Cross-tenant dashboard shows zeros with its own tenant scope."""
    supervisor_b = _supervisor(tenant_id=TENANT_B)
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", uuid4()))
    case = _build_case(tenant_id=TENANT_A)
    case_repo.seed(case)
    case_repo._open_case_ids = (case.id,)  # type: ignore[attr-defined]

    dashboard = await dashboards.operational_dashboard(
        dashboards.OperationalDashboardQuery(
            tenant_id=supervisor_b.tenant_id,
            actor_id=supervisor_b.user_id,
            actor_roles=supervisor_b.roles,
        ),
        case_repo=case_repo,
        task_repo=task_repo,
        queue_repo=queue_repo,
        now=NOW,
    )

    assert dashboard.kpis.total_open_cases == 0
    assert dashboard.kpis.total_overdue_tasks == 0
    assert dashboard.team == ()
    assert f"tenant:{TENANT_B}" in dashboard.authorization_scope


@pytest.mark.asyncio
async def test_dashboard_stale_served_at__rejected() -> None:
    """A served_at beyond the 5s freshness budget fails fast (stale guard)."""
    supervisor = _supervisor()
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(_queue(TENANT_A, "intake_queue", supervisor.user_id))

    with pytest.raises(ValueError, match="freshness window exceeded"):
        await dashboards.operational_dashboard(
            dashboards.OperationalDashboardQuery(
                tenant_id=supervisor.tenant_id,
                actor_id=supervisor.user_id,
                actor_roles=supervisor.roles,
            ),
            case_repo=case_repo,
            task_repo=task_repo,
            queue_repo=queue_repo,
            now=NOW,
            served_at=NOW + timedelta(seconds=30),
        )
