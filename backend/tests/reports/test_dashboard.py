"""Tests for the manager KPI dashboard (P12-US8 / FM-054).

Covers the acceptance criteria:

- KPIs reflect the current state of the system of record at the moment
  the snapshot was issued (``generated_at`` is the source-of-record-as-of
  timestamp).
- The dashboard serves sub-five-second freshness
  (``freshness_seconds <= 5``); stale data is never returned.
- Authorization filters the KPIs to the actor's team queues; a
  reviewer who is not a queue member sees zeros, never another team's
  counts.
- A missing ``case.read`` grant surfaces ``Forbidden``.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per
AGENTS.md §4.R5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.reports import dashboards, operational_reports
from app.deps import UserContext
from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.errors import Forbidden
from app.domain.queue import AssignmentPolicy, Queue

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
PAST_DUE = NOW - timedelta(hours=2)


# ---------------------------------------------------------------------------
# Fakes — local in-memory repositories that satisfy the Protocols.
# Mirrors test_operational_reports.py so the dashboard tests exercise
# the same wiring pattern as the existing reports suite.
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

    async def add(self, task: Any) -> None:
        self._tasks[(task.tenant_id, task.id)] = task

    async def update(self, task: Any) -> None:
        self._tasks[(task.tenant_id, task.id)] = task

    async def get(self, tenant_id: UUID, task_id: UUID) -> FakeTask | None:
        return self._tasks.get((tenant_id, task_id))

    async def list_open_for_queue(
        self,
        tenant_id: UUID,
        queue_key: str,
        *,
        limit: int = 100,
    ) -> list[FakeTask]:
        return [
            t
            for (tid, _), t in self._tasks.items()
            if tid == tenant_id and t.status == "open" and t.assignee_queue_id == queue_key
        ][:limit]

    async def list_open_for_user(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        limit: int = 100,
    ) -> list[FakeTask]:
        return [
            t
            for (tid, _), t in self._tasks.items()
            if tid == tenant_id and t.status == "open" and t.assignee_user_id == user_id
        ][:limit]

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

    async def add(self, case: Case) -> None:
        self._cases[(case.tenant_id, case.id)] = case

    async def update(self, case: Case) -> None:
        self._cases[(case.tenant_id, case.id)] = case

    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
        return self._cases.get((tenant_id, case_id))

    async def get_by_reference(self, tenant_id: UUID, reference: CaseReference) -> Case | None:
        for c in self._cases.values():
            if c.tenant_id == tenant_id and c.reference == reference:
                return c
        return None

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Case]:
        return list(self._cases.values())[:limit]

    async def next_reference(
        self, tenant_id: UUID, *, now: datetime, type_key: str | None = None
    ) -> CaseReference:
        return CaseReference(f"C-ACME-{now.year}-{(uuid4().int % 1_000_000):06d}")

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
        self._by_key: dict[tuple[UUID, str], Queue] = {}

    def seed(self, queue: Queue) -> None:
        self._queues[(queue.tenant_id, queue.id)] = queue
        self._by_key[(queue.tenant_id, queue.key)] = queue

    async def list_keys(self, tenant_id: UUID) -> list[str]:
        return sorted(q.key for (tid, _), q in self._queues.items() if tid == tenant_id)

    async def list_queues(self, tenant_id: UUID) -> list[Queue]:
        return sorted(
            (q for (tid, _), q in self._queues.items() if tid == tenant_id),
            key=lambda q: q.key,
        )

    async def get(self, tenant_id: UUID, queue_id: UUID) -> Queue | None:
        return self._queues.get((tenant_id, queue_id))

    async def get_by_key(self, tenant_id: UUID, key: str) -> Queue | None:
        return self._by_key.get((tenant_id, key))

    async def add(self, queue: Queue) -> None:
        self._queues[(queue.tenant_id, queue.id)] = queue
        self._by_key[(queue.tenant_id, queue.key)] = queue

    async def update(self, queue: Queue) -> None:
        self._queues[(queue.tenant_id, queue.id)] = queue
        self._by_key[(queue.tenant_id, queue.key)] = queue

    async def delete(self, tenant_id: UUID, queue_id: UUID) -> None:
        q = self._queues.pop((tenant_id, queue_id), None)
        if q is not None:
            self._by_key.pop((tenant_id, q.key), None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _supervisor(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant_id,
        display_name="supervisor",
        roles=("supervisor",),
    )


def _reviewer(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant_id,
        display_name="reviewer",
        roles=("reviewer",),
    )


def _build_case(
    *,
    tenant_id: UUID,
    case_id: UUID | None = None,
    status: CaseStatus = CaseStatus.OPEN,
    priority: str = "normal",
) -> Case:
    return Case(
        id=case_id or uuid4(),
        tenant_id=tenant_id,
        reference=CaseReference(f"C-ACME-{NOW.year}-{(uuid4().int % 1_000_000):06d}"),
        case_type_version_id=uuid4(),
        status=status,
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


# ---------------------------------------------------------------------------
# operational_dashboard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_returns_kpis_with_source_timestamp_and_sub_5s_freshness() -> None:
    """Manager dashboard reports KPIs from current state, served <5s.

    The supervisor has two open cases (one urgent, one normal) and one
    overdue task on the team queue. The dashboard reports those exact
    counts, the ``generated_at`` source timestamp, and a freshness
    window that fits inside the 5-second budget.
    """
    supervisor = _supervisor()
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()

    urgent = _build_case(tenant_id=TENANT_A, priority="urgent")
    normal = _build_case(tenant_id=TENANT_A, priority="normal")
    case_repo.seed(urgent)
    case_repo.seed(normal)
    case_repo._open_case_ids = (urgent.id, normal.id)  # type: ignore[attr-defined]

    queue = Queue.create(
        tenant_id=TENANT_A,
        key="intake_queue",
        name="Intake",
        member_user_ids=(supervisor.user_id,),
        assignment_policy=AssignmentPolicy.MANUAL,
    )
    queue_repo.seed(queue)

    task_repo.seed(
        FakeTask(
            id=uuid4(),
            tenant_id=TENANT_A,
            case_id=urgent.id,
            type="review",
            title="Past-due review",
            due_at=PAST_DUE,
            assignee_queue_id="intake_queue",
        )
    )

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
    )

    assert dashboard.kind == "snapshot"
    assert dashboard.generated_at == NOW
    assert dashboard.served_at == NOW
    assert dashboard.freshness_seconds == 0
    assert dashboard.freshness_seconds <= 5
    assert dashboard.team == ("intake_queue",)
    assert dashboard.kpis.total_open_cases == 2
    assert dashboard.kpis.total_overdue_tasks == 1
    assert dashboard.kpis.open_by_priority == {"urgent": 1, "normal": 1}


@pytest.mark.asyncio
async def test_dashboard_freshness_is_within_5_seconds_for_subsequent_served_at() -> None:
    """A ``served_at`` up to 5s after ``generated_at`` is honored.

    The use case enforces the FM-054 ceiling; a wall-clock gap inside
    the budget is reflected in ``freshness_seconds`` and the wire
    shape's ``ge=0, le=5`` constraint accepts it.
    """
    supervisor = _supervisor()
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(
        Queue.create(
            tenant_id=TENANT_A,
            key="intake_queue",
            name="Intake",
            member_user_ids=(supervisor.user_id,),
            assignment_policy=AssignmentPolicy.MANUAL,
        )
    )

    served_at = NOW + timedelta(seconds=3)
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
        served_at=served_at,
    )

    assert dashboard.freshness_seconds == 3
    assert dashboard.freshness_seconds <= 5
    assert dashboard.served_at == served_at


@pytest.mark.asyncio
async def test_dashboard_rejects_served_at_older_than_5s_budget() -> None:
    """A ``served_at`` past the 5-second budget raises ``ValueError``.

    The use case refuses to mint a dashboard whose freshness claim is
    a lie; the router never wraps this in a 200. The exception
    propagates so a misuse (e.g. a stale cached clock) fails fast at
    the use-case boundary.
    """
    supervisor = _supervisor()
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(
        Queue.create(
            tenant_id=TENANT_A,
            key="intake_queue",
            name="Intake",
            member_user_ids=(supervisor.user_id,),
            assignment_policy=AssignmentPolicy.MANUAL,
        )
    )

    stale = NOW + timedelta(seconds=30)
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
            served_at=stale,
        )


@pytest.mark.asyncio
async def test_dashboard_for_non_member_returns_zero_kpis_without_leaking() -> None:
    """A reviewer who is not a queue member sees zeros.

    Authorization grants ``case.read`` to reviewers (so the call is not
    denied) but the team scope is empty — the dashboard never
    confirms whether a queue exists in another team.
    """
    reviewer = _reviewer()
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()

    case = _build_case(tenant_id=TENANT_A)
    case_repo.seed(case)
    case_repo._open_case_ids = (case.id,)  # type: ignore[attr-defined]
    queue_repo.seed(
        Queue.create(
            tenant_id=TENANT_A,
            key="intake_queue",
            name="Intake",
            member_user_ids=(uuid4(),),
            assignment_policy=AssignmentPolicy.MANUAL,
        )
    )

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
    )

    assert dashboard.kpis.total_open_cases == 0
    assert dashboard.kpis.total_overdue_tasks == 0
    assert dashboard.kpis.open_by_priority == {}
    assert dashboard.team == ()
    assert dashboard.generated_at == NOW
    assert dashboard.freshness_seconds <= 5


@pytest.mark.asyncio
async def test_dashboard_for_actor_without_role_is_forbidden() -> None:
    """Deny-by-default: missing ``case.read`` raises ``Forbidden``."""
    actor = UserContext(
        user_id=uuid4(),
        tenant_id=TENANT_A,
        display_name="anon",
        roles=(),
    )
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()
    queue_repo.seed(
        Queue.create(
            tenant_id=TENANT_A,
            key="intake_queue",
            name="Intake",
            member_user_ids=(actor.user_id,),
            assignment_policy=AssignmentPolicy.MANUAL,
        )
    )

    with pytest.raises(Forbidden):
        await dashboards.operational_dashboard(
            dashboards.OperationalDashboardQuery(
                tenant_id=actor.tenant_id,
                actor_id=actor.user_id,
                actor_roles=actor.roles,
            ),
            case_repo=case_repo,
            task_repo=task_repo,
            queue_repo=queue_repo,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_dashboard_requires_task_read_in_addition_to_case_read() -> None:
    """A case-readable actor cannot infer task KPIs without task.read."""
    actor = UserContext(
        user_id=uuid4(),
        tenant_id=TENANT_A,
        display_name="intake",
        roles=("intake",),
    )
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()

    with pytest.raises(Forbidden, match="task.read"):
        await dashboards.operational_dashboard(
            dashboards.OperationalDashboardQuery(
                tenant_id=actor.tenant_id,
                actor_id=actor.user_id,
                actor_roles=actor.roles,
            ),
            case_repo=case_repo,
            task_repo=task_repo,
            queue_repo=queue_repo,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_dashboard_does_not_leak_cross_tenant_work() -> None:
    """A cross-tenant supervisor cannot see another tenant's rows.

    The delegated snapshot reads resolve the team through the
    tenant-scoped queue repository; a queue in tenant B is invisible
    from tenant A and the KPIs collapse to zero.
    """
    supervisor_b = UserContext(
        user_id=uuid4(),
        tenant_id=TENANT_B,
        display_name="supervisor-b",
        roles=("supervisor",),
    )
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()

    queue_repo.seed(
        Queue.create(
            tenant_id=TENANT_A,
            key="intake_queue",
            name="Intake",
            member_user_ids=(uuid4(),),
            assignment_policy=AssignmentPolicy.MANUAL,
        )
    )
    case = _build_case(tenant_id=TENANT_A)
    case_repo.seed(case)
    case_repo._open_case_ids = (case.id,)  # type: ignore[attr-defined]
    task_repo.seed(
        FakeTask(
            id=uuid4(),
            tenant_id=TENANT_A,
            case_id=case.id,
            type="review",
            title="Tenant A past due",
            due_at=PAST_DUE,
            assignee_queue_id="intake_queue",
        )
    )

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


@pytest.mark.asyncio
async def test_dashboard_reflects_state_change_between_calls() -> None:
    """Two consecutive calls reflect different system-of-record states.

    The dashboard must read the live repositories on every call; a
    cache that returned the first call's numbers after the underlying
    state changed would violate the freshness contract. This test
    also exercises the "freshness under 5s" promise on the second
    call by passing a ``served_at`` 4 seconds after the first.
    """
    supervisor = _supervisor()
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()

    queue = Queue.create(
        tenant_id=TENANT_A,
        key="intake_queue",
        name="Intake",
        member_user_ids=(supervisor.user_id,),
        assignment_policy=AssignmentPolicy.MANUAL,
    )
    queue_repo.seed(queue)

    case_a = _build_case(tenant_id=TENANT_A)
    case_repo.seed(case_a)
    case_repo._open_case_ids = (case_a.id,)  # type: ignore[attr-defined]

    first = await dashboards.operational_dashboard(
        dashboards.OperationalDashboardQuery(
            tenant_id=supervisor.tenant_id,
            actor_id=supervisor.user_id,
            actor_roles=supervisor.roles,
        ),
        case_repo=case_repo,
        task_repo=task_repo,
        queue_repo=queue_repo,
        now=NOW,
    )
    assert first.kpis.total_open_cases == 1
    assert first.kpis.total_overdue_tasks == 0

    # Simulate a second open case landing before the supervisor's
    # next refresh.
    case_b = _build_case(tenant_id=TENANT_A)
    case_repo.seed(case_b)
    case_repo._open_case_ids = (case_a.id, case_b.id)  # type: ignore[attr-defined]

    served_after = NOW + timedelta(seconds=4)
    second = await dashboards.operational_dashboard(
        dashboards.OperationalDashboardQuery(
            tenant_id=supervisor.tenant_id,
            actor_id=supervisor.user_id,
            actor_roles=supervisor.roles,
        ),
        case_repo=case_repo,
        task_repo=task_repo,
        queue_repo=queue_repo,
        now=NOW,
        served_at=served_after,
    )
    assert second.kpis.total_open_cases == 2
    # ``generated_at`` is the same wall-clock moment (same ``now``),
    # but the dashboard read the live state and observed the new case.
    assert second.generated_at == first.generated_at == NOW
    assert second.freshness_seconds == 4
    assert second.freshness_seconds <= 5


@pytest.mark.asyncio
async def test_dashboard_open_by_priority_buckets_empty_priority() -> None:
    """A missing / empty priority value buckets under ``"other"``.

    A future priority value (e.g. ``"critical"``) is a valid label and
    must not be re-mapped under ``"other"``; only a missing or empty
    priority is bucketed under ``"other"`` so the caller can still
    trust ``total_open_cases`` to match the sum.
    """
    supervisor = _supervisor()
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()

    queue = Queue.create(
        tenant_id=TENANT_A,
        key="intake_queue",
        name="Intake",
        member_user_ids=(supervisor.user_id,),
        assignment_policy=AssignmentPolicy.MANUAL,
    )
    queue_repo.seed(queue)

    known = _build_case(tenant_id=TENANT_A, priority="high")
    unlabeled = _build_case(tenant_id=TENANT_A, priority="")
    case_repo.seed(known)
    case_repo.seed(unlabeled)
    case_repo._open_case_ids = (known.id, unlabeled.id)  # type: ignore[attr-defined]

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
    )

    assert dashboard.kpis.total_open_cases == 2
    assert dashboard.kpis.open_by_priority == {"high": 1, "other": 1}
    assert sum(dashboard.kpis.open_by_priority.values()) == dashboard.kpis.total_open_cases


# ---------------------------------------------------------------------------
# Sanity: the existing operational reports are not regressed
# ---------------------------------------------------------------------------


def test_dashboard_module_does_not_shadow_operational_reports() -> None:
    """Importing the dashboard module keeps the operational module intact.

    A regression where the dashboard module replaced
    :mod:`operational_reports` would silently break the existing
    reports; this test pins the import graph.
    """
    assert operational_reports.open_cases is not None
    assert operational_reports.overdue_tasks is not None
    assert dashboards.operational_dashboard is not None
