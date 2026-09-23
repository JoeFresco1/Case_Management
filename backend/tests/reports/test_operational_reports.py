"""Tests for operational reports (P12-US1, US2 / FM-051).

Covers the acceptance criteria:

- Supervisor (queue member) sees their team's open cases and overdue
  tasks; the response carries a ``generated_at`` source timestamp.
- Overdue is filtered correctly: only open tasks whose ``due_at`` is
  at or before the report time are included; future-dated and
  already-completed tasks are excluded.
- Non-team-member (a reviewer who is not in the queue membership
  list) cannot see other teams' tasks — the report returns an empty
  result set rather than leaking existence (Constitution V).
- Authorization: an actor missing ``case.read`` is denied.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per
AGENTS.md §4.R5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.reports import operational_reports
from app.deps import UserContext
from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.errors import Forbidden, ValidationFailed
from app.domain.queue import AssignmentPolicy, Queue

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
PAST_DUE = NOW - timedelta(hours=2)
FUTURE_DUE = NOW + timedelta(hours=4)


# ---------------------------------------------------------------------------
# Fakes — local in-memory repositories that satisfy the Protocols.
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
        # Match by case_id being one of the seeded case ids — the test
        # layer wires the join through ``open_case_ids`` on the repo.
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


def _other_tenant_supervisor() -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=TENANT_B,
        display_name="supervisor-b",
        roles=("supervisor",),
    )


def _build_case(
    *,
    tenant_id: UUID,
    case_id: UUID | None = None,
    status: CaseStatus = CaseStatus.OPEN,
) -> Case:
    """Construct a minimal Case aggregate for tests.

    Returns a dataclass instance satisfying the attribute shape the
    use case reads (``id``, ``tenant_id``, ``reference``,
    ``case_type_version_id``, ``status``, ``priority``, ``title``,
    ``current_workflow_state``, ``created_at``, ``updated_at``).
    """
    ctv_id = uuid4()
    return Case(
        id=case_id or uuid4(),
        tenant_id=tenant_id,
        reference=CaseReference(f"C-ACME-{NOW.year}-{(uuid4().int % 1_000_000):06d}"),
        case_type_version_id=ctv_id,
        status=status,
        priority="normal",
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
# open_cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_cases_for_supervisor_returns_team_scoped_rows_with_source_timestamp() -> None:
    """Supervisor sees open cases for queues they belong to.

    The response carries ``generated_at`` (the source timestamp) and
    the resolved ``team`` set; a case tied to a queue the supervisor
    is not a member of is invisible.
    """
    supervisor = _supervisor()
    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()

    in_team = _build_case(tenant_id=TENANT_A)
    out_of_team = _build_case(tenant_id=TENANT_A)
    case_repo.seed(in_team)
    case_repo.seed(out_of_team)
    case_repo._open_case_ids = (in_team.id,)  # type: ignore[attr-defined]

    team_queue = Queue.create(
        tenant_id=TENANT_A,
        key="intake_queue",
        name="Intake",
        member_user_ids=(supervisor.user_id,),
        assignment_policy=AssignmentPolicy.MANUAL,
    )
    other_queue = Queue.create(
        tenant_id=TENANT_A,
        key="other_queue",
        name="Other",
        member_user_ids=(uuid4(),),
        assignment_policy=AssignmentPolicy.MANUAL,
    )
    queue_repo.seed(team_queue)
    queue_repo.seed(other_queue)

    report = await operational_reports.open_cases(
        operational_reports.OperationalReportsQuery(
            tenant_id=supervisor.tenant_id,
            actor_id=supervisor.user_id,
            actor_roles=supervisor.roles,
        ),
        case_repo=case_repo,
        queue_repo=queue_repo,
        now=NOW,
    )

    assert report.generated_at == NOW
    assert report.total == 1
    assert report.team == ("intake_queue",)
    assert report.items[0].case_id == in_team.id


@pytest.mark.asyncio
async def test_open_cases_for_non_member_returns_empty_report_without_leaking() -> None:
    """A reviewer who is not a queue member sees no rows.

    Authorization grants ``case.read`` to reviewers (so the call is
    not denied) but the team scope is empty — the report never
    confirms whether a queue exists in another team.
    """
    reviewer = _reviewer()
    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()

    case = _build_case(tenant_id=TENANT_A)
    case_repo.seed(case)
    case_repo._open_case_ids = (case.id,)  # type: ignore[attr-defined]

    queue = Queue.create(
        tenant_id=TENANT_A,
        key="intake_queue",
        name="Intake",
        member_user_ids=(uuid4(),),
        assignment_policy=AssignmentPolicy.MANUAL,
    )
    queue_repo.seed(queue)

    report = await operational_reports.open_cases(
        operational_reports.OperationalReportsQuery(
            tenant_id=reviewer.tenant_id,
            actor_id=reviewer.user_id,
            actor_roles=reviewer.roles,
        ),
        case_repo=case_repo,
        queue_repo=queue_repo,
        now=NOW,
    )

    assert report.total == 0
    assert report.items == ()
    assert report.team == ()
    # Source timestamp is still populated even on an empty report so
    # the UI can show "as of" honestly.
    assert report.generated_at == NOW


@pytest.mark.asyncio
async def test_open_cases_for_actor_without_role_is_forbidden() -> None:
    """Deny-by-default: an actor without ``case.read`` is denied.

    ``Policy()`` with no role grants returns ``Decision.deny`` for
    every action; the use case surfaces ``Forbidden`` so the HTTP
    layer can translate to 403.
    """
    actor = UserContext(
        user_id=uuid4(),
        tenant_id=TENANT_A,
        display_name="anon",
        roles=(),  # no roles
    )
    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()
    # Seed a queue so we get past authorization for the queue
    # listing (the use case authorizes against ``case.read`` first;
    # a missing grant raises ``Forbidden`` before the queue read).
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
        await operational_reports.open_cases(
            operational_reports.OperationalReportsQuery(
                tenant_id=actor.tenant_id,
                actor_id=actor.user_id,
                actor_roles=actor.roles,
            ),
            case_repo=case_repo,
            queue_repo=queue_repo,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_open_cases_for_actor_with_no_team_memberships_returns_empty() -> None:
    """A supervisor with no queue memberships sees an empty report.

    The supervisor is authorized (they hold ``case.read``) but their
    team set is empty. The report is well-formed: ``total=0``,
    ``team=()``, ``generated_at`` populated.
    """
    supervisor = _supervisor()
    case_repo = InMemoryCaseRepository()
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

    report = await operational_reports.open_cases(
        operational_reports.OperationalReportsQuery(
            tenant_id=supervisor.tenant_id,
            actor_id=supervisor.user_id,
            actor_roles=supervisor.roles,
        ),
        case_repo=case_repo,
        queue_repo=queue_repo,
        now=NOW,
    )

    assert report.total == 0
    assert report.team == ()
    assert report.generated_at == NOW


@pytest.mark.asyncio
async def test_open_cases_rejects_out_of_range_limit() -> None:
    """Limit must be in ``[1, 200]``; anything else raises ``ValidationFailed``."""
    supervisor = _supervisor()
    case_repo = InMemoryCaseRepository()
    queue_repo = InMemoryQueueRepository()

    with pytest.raises(ValidationFailed):
        await operational_reports.open_cases(
            operational_reports.OperationalReportsQuery(
                tenant_id=supervisor.tenant_id,
                actor_id=supervisor.user_id,
                actor_roles=supervisor.roles,
            ),
            case_repo=case_repo,
            queue_repo=queue_repo,
            now=NOW,
            limit=0,
        )


# ---------------------------------------------------------------------------
# overdue_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overdue_tasks_filters_to_overdue_only_with_source_timestamp() -> None:
    """Only open tasks with ``due_at <= now`` in the team queues are included.

    Future-dated, completed, and out-of-team tasks are excluded. The
    response carries ``generated_at`` and the team set.
    """
    supervisor = _supervisor()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()

    team_queue = Queue.create(
        tenant_id=TENANT_A,
        key="intake_queue",
        name="Intake",
        member_user_ids=(supervisor.user_id,),
        assignment_policy=AssignmentPolicy.MANUAL,
    )
    other_queue = Queue.create(
        tenant_id=TENANT_A,
        key="other_queue",
        name="Other",
        member_user_ids=(uuid4(),),
        assignment_policy=AssignmentPolicy.MANUAL,
    )
    queue_repo.seed(team_queue)
    queue_repo.seed(other_queue)

    case_id = uuid4()
    overdue = task_repo.seed(
        FakeTask(
            id=uuid4(),
            tenant_id=TENANT_A,
            case_id=case_id,
            type="review",
            title="Past-due review",
            due_at=PAST_DUE,
            assignee_queue_id="intake_queue",
        )
    )
    future = task_repo.seed(
        FakeTask(
            id=uuid4(),
            tenant_id=TENANT_A,
            case_id=case_id,
            type="review",
            title="Future review",
            due_at=FUTURE_DUE,
            assignee_queue_id="intake_queue",
        )
    )
    completed = task_repo.seed(
        FakeTask(
            id=uuid4(),
            tenant_id=TENANT_A,
            case_id=case_id,
            type="review",
            title="Completed but past due",
            status="completed",
            due_at=PAST_DUE,
            assignee_queue_id="intake_queue",
        )
    )
    out_of_team = task_repo.seed(
        FakeTask(
            id=uuid4(),
            tenant_id=TENANT_A,
            case_id=case_id,
            type="review",
            title="Other team past due",
            due_at=PAST_DUE,
            assignee_queue_id="other_queue",
        )
    )
    _ = (future, completed, out_of_team)

    report = await operational_reports.overdue_tasks(
        operational_reports.OperationalReportsQuery(
            tenant_id=supervisor.tenant_id,
            actor_id=supervisor.user_id,
            actor_roles=supervisor.roles,
        ),
        task_repo=task_repo,
        queue_repo=queue_repo,
        now=NOW,
    )

    assert report.generated_at == NOW
    assert report.team == ("intake_queue",)
    assert report.total == 1
    assert report.items[0].task_id == overdue.id
    assert report.items[0].overdue_seconds > 0


@pytest.mark.asyncio
async def test_overdue_tasks_non_team_member_returns_empty_report() -> None:
    """A reviewer who is not in the queue membership list sees no rows.

    The reviewer is authorized (they hold ``case.read``) but their
    team set is empty — the report never confirms whether a queue
    exists in another team (Constitution V).
    """
    reviewer = _reviewer()
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
    task_repo.seed(
        FakeTask(
            id=uuid4(),
            tenant_id=TENANT_A,
            case_id=uuid4(),
            type="review",
            title="Past-due review",
            due_at=PAST_DUE,
            assignee_queue_id="intake_queue",
        )
    )

    report = await operational_reports.overdue_tasks(
        operational_reports.OperationalReportsQuery(
            tenant_id=reviewer.tenant_id,
            actor_id=reviewer.user_id,
            actor_roles=reviewer.roles,
        ),
        task_repo=task_repo,
        queue_repo=queue_repo,
        now=NOW,
    )

    assert report.total == 0
    assert report.items == ()
    assert report.team == ()


@pytest.mark.asyncio
async def test_overdue_tasks_for_actor_without_role_is_forbidden() -> None:
    """Deny-by-default: missing ``case.read`` raises ``Forbidden``."""
    actor = UserContext(
        user_id=uuid4(),
        tenant_id=TENANT_A,
        display_name="anon",
        roles=(),
    )
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
        await operational_reports.overdue_tasks(
            operational_reports.OperationalReportsQuery(
                tenant_id=actor.tenant_id,
                actor_id=actor.user_id,
                actor_roles=actor.roles,
            ),
            task_repo=task_repo,
            queue_repo=queue_repo,
            now=NOW,
        )


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reports_do_not_leak_cross_tenant_work() -> None:
    """A cross-tenant supervisor cannot see another tenant's rows.

    The use case resolves the team through the tenant-scoped
    ``QueueRepository.list_queues``; a queue in tenant B is invisible
    from tenant A and the supervisor's team set is empty.
    """
    supervisor_b = _other_tenant_supervisor()
    task_repo = InMemoryTaskRepository()
    queue_repo = InMemoryQueueRepository()

    # Tenant A queue + task — should not appear in the B-side report.
    queue_repo.seed(
        Queue.create(
            tenant_id=TENANT_A,
            key="intake_queue",
            name="Intake",
            member_user_ids=(uuid4(),),
            assignment_policy=AssignmentPolicy.MANUAL,
        )
    )
    task_repo.seed(
        FakeTask(
            id=uuid4(),
            tenant_id=TENANT_A,
            case_id=uuid4(),
            type="review",
            title="Tenant A past due",
            due_at=PAST_DUE,
            assignee_queue_id="intake_queue",
        )
    )

    report = await operational_reports.overdue_tasks(
        operational_reports.OperationalReportsQuery(
            tenant_id=supervisor_b.tenant_id,
            actor_id=supervisor_b.user_id,
            actor_roles=supervisor_b.roles,
        ),
        task_repo=task_repo,
        queue_repo=queue_repo,
        now=NOW,
    )

    assert report.total == 0
    assert report.team == ()
