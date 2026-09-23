"""Queue scope, aging, and delegation-expiry tests (P02-US3, US9 / FM-016).

Covers the T4 queue-domain slice that ``test_queue_admin.py`` does not:

- Membership and visibility scope: ``TENANT`` queues list to every
  tenant user, ``MEMBERS`` queues list only to members, admins always
  see every queue; ``define`` / ``reconfigure`` round-trip the scope.
- Due-date and aging context: the pure ``task_aging_context`` readout
  (age, overdue flag + lateness, time-to-due) including the no-due-date
  and missing-timestamp edges.
- Time-bounded delegation with expiry-restore: ``delegate`` records a
  ``Delegation`` per task; ``expire_delegations`` hands the task back
  to its prior owner once ``now > until`` (one audit row + one outbox
  event per restore), drops already-settled records as superseded,
  and never touches unexpired or cross-tenant records.
- Human-driven workflow cycles stay allowed while automatic runaway
  loops stay rejected (validator regression pin for this task's
  acceptance criterion).

Naming follows ``test_<unit>_<scenario>__<expectation>`` per
AGENTS.md §4.R5. Fakes are local to this file (mirroring
``test_queue_admin.py``) so sibling edits to that file cannot break
this suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.configuration.validate_definition import (
    validate_definition,
)
from app.application.queues import manage_queues
from app.deps import UserContext
from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.queue import (
    AssignmentPolicy,
    Delegation,
    Queue,
    VisibilityScope,
    task_aging_context,
    visible_queues_for,
)
from app.domain.workflow import (
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes — local, mirror ``tests/application/test_queue_admin.py`` style.
# ---------------------------------------------------------------------------


class FixedClock:
    """Controllable clock; tests advance ``now`` past delegation bounds."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or NOW

    def now(self) -> datetime:
        return self._now

    def set_now(self, now: datetime) -> None:
        self._now = now


@dataclass
class StagedAudit:
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    subject_type: str
    subject_id: UUID
    payload: dict[str, Any]
    occurred_at: datetime
    correlation_id: str = ""
    case_id: UUID | None = None


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.events: list[StagedAudit] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        _ = (actor_type, causation_id)
        self.events.append(
            StagedAudit(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                subject_type=subject_type,
                subject_id=subject_id,
                payload=payload,
                occurred_at=occurred_at,
                correlation_id=correlation_id,
                case_id=case_id,
            )
        )


@dataclass
class StagedOutbox:
    tenant_id: UUID
    event_type: str
    payload: dict[str, Any]


class InMemoryOutboxWriter:
    def __init__(self) -> None:
        self.events: list[StagedOutbox] = []

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        _ = event_id
        self.events.append(
            StagedOutbox(
                tenant_id=tenant_id,
                event_type=event_type,
                payload=payload,
            )
        )


class InMemoryQueueRepository:
    def __init__(self) -> None:
        self._queues: dict[tuple[UUID, UUID], Queue] = {}
        self._queues_by_key: dict[tuple[UUID, str], Queue] = {}

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
        return self._queues_by_key.get((tenant_id, key))

    async def add(self, queue: Queue) -> None:
        self._queues[(queue.tenant_id, queue.id)] = queue
        self._queues_by_key[(queue.tenant_id, queue.key)] = queue

    async def update(self, queue: Queue) -> None:
        self._queues[(queue.tenant_id, queue.id)] = queue
        self._queues_by_key[(queue.tenant_id, queue.key)] = queue

    async def delete(self, tenant_id: UUID, queue_id: UUID) -> None:
        q = self._queues.pop((tenant_id, queue_id), None)
        if q is not None:
            self._queues_by_key.pop((tenant_id, q.key), None)


@dataclass
class FakeTask:
    """In-memory stand-in for ``Task`` (adds ``status`` for expiry)."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    type: str
    title: str
    status: str = "open"
    assignee_user_id: UUID | None = None
    assignee_queue_id: str | None = None
    due_at: datetime | None = None
    created_at: datetime | None = field(default_factory=lambda: NOW)
    version_token: int = 1


class InMemoryTaskRepository:
    def __init__(self) -> None:
        self._tasks: dict[tuple[UUID, UUID], FakeTask] = {}

    def seed(self, task: FakeTask) -> FakeTask:
        self._tasks[(task.tenant_id, task.id)] = task
        return task

    async def get(self, tenant_id: UUID, task_id: UUID) -> FakeTask | None:
        return self._tasks.get((tenant_id, task_id))

    async def update(self, task: FakeTask) -> None:
        self._tasks[(task.tenant_id, task.id)] = task

    async def list_open_for_queue(
        self,
        tenant_id: UUID,
        queue_key: str,
        *,
        limit: int = 100,
    ) -> list[FakeTask]:
        return sorted(
            (
                t
                for (tid, _), t in self._tasks.items()
                if tid == tenant_id and t.status == "open" and t.assignee_queue_id == queue_key
            ),
            key=lambda t: (t.due_at is None, t.due_at or NOW, t.created_at or NOW),
        )[:limit]

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


class InMemoryDelegationStore:
    """Tenant-scoped active-delegation set (no tables in this task)."""

    def __init__(self) -> None:
        self._records: dict[tuple[UUID, UUID], Delegation] = {}

    async def record(self, delegation: Delegation) -> None:
        self._records[(delegation.tenant_id, delegation.task_id)] = delegation

    async def list_active(self, tenant_id: UUID) -> list[Delegation]:
        return sorted(
            (d for (tid, _), d in self._records.items() if tid == tenant_id),
            key=lambda d: d.delegated_at,
        )

    async def remove(self, tenant_id: UUID, task_id: UUID) -> None:
        self._records.pop((tenant_id, task_id), None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_user(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant_id,
        display_name="admin",
        roles=("process_admin",),
    )


def _worker_user(tenant_id: UUID = TENANT_A) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=tenant_id,
        display_name="worker",
        roles=("reviewer",),
    )


def _new_task(
    *,
    tenant_id: UUID = TENANT_A,
    owner_id: UUID | None = None,
    queue_key: str | None = "review_queue",
    due_at: datetime | None = None,
    created_at: datetime | None = NOW,
) -> FakeTask:
    return FakeTask(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=uuid4(),
        type="review",
        title="Review intake",
        assignee_user_id=owner_id,
        assignee_queue_id=queue_key if owner_id is None else None,
        due_at=due_at,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# 1. Visibility scope (domain)
# ---------------------------------------------------------------------------


def test_queue_visibility_tenant_scope__visible_to_every_tenant_user() -> None:
    member = _worker_user()
    outsider = _worker_user()
    queue = Queue.create(
        tenant_id=TENANT_A,
        key="open_queue",
        name="Open",
        member_user_ids=(member.user_id,),
        visibility_scope=VisibilityScope.TENANT,
    )

    assert queue.is_visible_to(member.user_id) is True
    assert queue.is_visible_to(outsider.user_id) is True


def test_queue_visibility_members_scope__hidden_from_non_member() -> None:
    member = _worker_user()
    outsider = _worker_user()
    queue = Queue.create(
        tenant_id=TENANT_A,
        key="closed_queue",
        name="Closed",
        member_user_ids=(member.user_id,),
        visibility_scope=VisibilityScope.MEMBERS,
    )

    assert queue.is_visible_to(member.user_id) is True
    assert queue.is_visible_to(outsider.user_id) is False


def test_queue_visibility_members_scope__admin_sees_every_queue() -> None:
    outsider = _worker_user()
    queue = Queue.create(
        tenant_id=TENANT_A,
        key="closed_queue",
        name="Closed",
        member_user_ids=(uuid4(),),
        visibility_scope=VisibilityScope.MEMBERS,
    )

    assert queue.is_visible_to(outsider.user_id, is_admin=True) is True


def test_visible_queues_for__filters_members_queues_preserving_order() -> None:
    member = _worker_user()
    outsider = _worker_user()
    open_queue = Queue.create(
        tenant_id=TENANT_A, key="a_open", name="Open", visibility_scope=VisibilityScope.TENANT
    )
    closed_queue = Queue.create(
        tenant_id=TENANT_A,
        key="b_closed",
        name="Closed",
        member_user_ids=(member.user_id,),
        visibility_scope=VisibilityScope.MEMBERS,
    )

    assert visible_queues_for([open_queue, closed_queue], outsider.user_id) == (open_queue,)
    assert visible_queues_for([open_queue, closed_queue], member.user_id) == (
        open_queue,
        closed_queue,
    )


def test_set_visibility_scope__bumps_token() -> None:
    queue = Queue.create(tenant_id=TENANT_A, key="q", name="Q")
    token_before = queue.version_token

    narrowed = queue.set_visibility_scope(visibility_scope=VisibilityScope.MEMBERS)

    assert narrowed.visibility_scope is VisibilityScope.MEMBERS
    assert narrowed.version_token == token_before + 1


def test_set_visibility_scope__rejects_non_scope_value() -> None:
    queue = Queue.create(tenant_id=TENANT_A, key="q", name="Q")

    with pytest.raises(InvariantViolation):
        queue.set_visibility_scope(visibility_scope="everyone")  # type: ignore[arg-type]


def test_create_queue__defaults_to_tenant_visible() -> None:
    # Why pinned: pre-scope rows (and SQL rows before the B-5 column
    # lands) must stay tenant-visible — scope can only narrow access,
    # never widen it (fail-closed, Constitution V).
    queue = Queue.create(tenant_id=TENANT_A, key="legacy", name="Legacy")

    assert queue.visibility_scope is VisibilityScope.TENANT


# ---------------------------------------------------------------------------
# 2. Visibility scope (application round-trip)
# ---------------------------------------------------------------------------


async def test_define_queue__persists_members_visibility_scope() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    admin = _admin_user()
    member = _worker_user()

    queue = await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid",
            key="pto_cover",
            name="PTO cover",
            assignment_policy=AssignmentPolicy.MANUAL,
            member_user_ids=(member.user_id,),
            visibility_scope=VisibilityScope.MEMBERS,
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert queue.visibility_scope is VisibilityScope.MEMBERS
    stored = await repo.get_by_key(admin.tenant_id, "pto_cover")
    assert stored is not None
    assert stored.is_visible_to(member.user_id) is True
    assert stored.is_visible_to(uuid4()) is False
    assert audit.events[0].payload["visibility_scope"] == "members"


async def test_reconfigure_queue__visibility_scope_change_is_audited() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    admin = _admin_user()

    queue = await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid-define",
            key="team_queue",
            name="Team",
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert queue.visibility_scope is VisibilityScope.TENANT

    updated = await manage_queues.reconfigure_queue(
        manage_queues.ReconfigureQueueCommand(
            tenant_id=admin.tenant_id,
            actor=admin,
            correlation_id="cid-reconfigure",
            queue_id=queue.id,
            visibility_scope=VisibilityScope.MEMBERS,
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert updated.visibility_scope is VisibilityScope.MEMBERS
    reconfigure_events = [
        e for e in audit.events if e.event_type == manage_queues.QUEUE_RECONFIGURED
    ]
    assert len(reconfigure_events) == 1
    assert reconfigure_events[0].payload["visibility_scope"] == "members"


# ---------------------------------------------------------------------------
# 3. Aging context (domain, pure)
# ---------------------------------------------------------------------------


def test_task_aging_overdue_task__flags_lateness() -> None:
    task = _new_task(due_at=NOW - timedelta(hours=3), created_at=NOW - timedelta(days=2))

    aging = task_aging_context(task, now=NOW)

    assert aging.is_overdue is True
    assert aging.overdue_by_seconds == pytest.approx(3 * 3600)
    assert aging.due_in_seconds is None
    assert aging.age_seconds == pytest.approx(2 * 86400)


def test_task_aging_upcoming_due__reports_due_in() -> None:
    task = _new_task(due_at=NOW + timedelta(hours=5), created_at=NOW - timedelta(hours=1))

    aging = task_aging_context(task, now=NOW)

    assert aging.is_overdue is False
    assert aging.overdue_by_seconds is None
    assert aging.due_in_seconds == pytest.approx(5 * 3600)
    assert aging.age_seconds == pytest.approx(3600)


def test_task_aging_no_due_date__never_overdue() -> None:
    task = _new_task(due_at=None, created_at=NOW - timedelta(hours=2))

    aging = task_aging_context(task, now=NOW)

    assert aging.is_overdue is False
    assert aging.overdue_by_seconds is None
    assert aging.due_in_seconds is None
    assert aging.age_seconds == pytest.approx(7200)


def test_task_aging_missing_created_at__age_floors_at_zero() -> None:
    task = _new_task(due_at=None, created_at=None)

    aging = task_aging_context(task, now=NOW)

    assert aging.age_seconds == 0.0
    assert aging.is_overdue is False


# ---------------------------------------------------------------------------
# 4. Delegation records (domain validation)
# ---------------------------------------------------------------------------


def test_delegation_create__rejects_zero_length_window_and_self() -> None:
    owner = uuid4()
    delegate = uuid4()

    with pytest.raises(InvariantViolation):
        Delegation.create(
            tenant_id=TENANT_A,
            task_id=uuid4(),
            case_id=uuid4(),
            from_user_id=owner,
            to_user_id=delegate,
            delegated_at=NOW,
            until=NOW,
        )

    with pytest.raises(InvariantViolation):
        Delegation.create(
            tenant_id=TENANT_A,
            task_id=uuid4(),
            case_id=uuid4(),
            from_user_id=owner,
            to_user_id=owner,
            delegated_at=NOW,
            until=NOW + timedelta(days=1),
        )


def test_delegation_is_expired__bound_is_exclusive() -> None:
    record = Delegation.create(
        tenant_id=TENANT_A,
        task_id=uuid4(),
        case_id=uuid4(),
        from_user_id=uuid4(),
        to_user_id=uuid4(),
        delegated_at=NOW,
        until=NOW + timedelta(days=7),
    )

    assert record.is_expired(now=NOW + timedelta(days=7)) is False
    assert record.is_expired(now=NOW + timedelta(days=7, seconds=1)) is True


# ---------------------------------------------------------------------------
# 5. Delegation expiry restores the prior owner (application)
# ---------------------------------------------------------------------------


async def test_expire_delegations__restores_prior_owner_after_until() -> None:
    tasks = InMemoryTaskRepository()
    delegations = InMemoryDelegationStore()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()

    owner = _worker_user()
    delegate = _worker_user()
    owned = replace(_new_task(tenant_id=owner.tenant_id), assignee_user_id=owner.user_id)
    tasks.seed(owned)
    until = NOW + timedelta(days=7)

    delegated = await manage_queues.delegate(
        manage_queues.DelegateCommand(
            tenant_id=owner.tenant_id,
            actor=owner,
            correlation_id="cid-delegate",
            delegate_user_id=delegate.user_id,
            task_ids=(owned.id,),
            until=until,
        ),
        task_repo=tasks,
        audit=audit,
        outbox=outbox,
        clock=clock,
        delegation_store=delegations,
    )
    assert len(delegated) == 1
    assert (await delegations.list_active(owner.tenant_id)) != []

    # given: the window passes with no human action
    clock.set_now(until + timedelta(seconds=1))

    # when: the system sweep runs
    result = await manage_queues.expire_delegations(
        manage_queues.ExpireDelegationsCommand(
            tenant_id=owner.tenant_id,
            correlation_id="cid-expire",
        ),
        task_repo=tasks,
        delegation_store=delegations,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # then: the prior owner is restored, audited, and emitted.
    assert len(result.restored) == 1
    assert result.superseded == ()
    current = await tasks.get(owner.tenant_id, owned.id)
    assert current is not None
    assert current.assignee_user_id == owner.user_id

    expiry_audits = [
        e for e in audit.events if e.event_type == manage_queues.TASK_DELEGATION_EXPIRED
    ]
    assert len(expiry_audits) == 1
    assert expiry_audits[0].payload["restored_user_id"] == str(owner.user_id)
    assert expiry_audits[0].payload["delegate_user_id"] == str(delegate.user_id)
    assert any(e.event_type == manage_queues.TASK_DELEGATION_EXPIRED for e in outbox.events)

    # …and the record is gone so a second sweep is a no-op.
    assert await delegations.list_active(owner.tenant_id) == []
    second = await manage_queues.expire_delegations(
        manage_queues.ExpireDelegationsCommand(
            tenant_id=owner.tenant_id,
            correlation_id="cid-expire-2",
        ),
        task_repo=tasks,
        delegation_store=delegations,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert second.restored == ()
    assert second.superseded == ()


async def test_expire_delegations_before_until__leaves_assignment_untouched() -> None:
    tasks = InMemoryTaskRepository()
    delegations = InMemoryDelegationStore()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()

    owner = _worker_user()
    delegate = _worker_user()
    owned = replace(_new_task(tenant_id=owner.tenant_id), assignee_user_id=owner.user_id)
    tasks.seed(owned)

    await manage_queues.delegate(
        manage_queues.DelegateCommand(
            tenant_id=owner.tenant_id,
            actor=owner,
            correlation_id="cid-delegate",
            delegate_user_id=delegate.user_id,
            task_ids=(owned.id,),
            until=NOW + timedelta(days=7),
        ),
        task_repo=tasks,
        audit=audit,
        outbox=outbox,
        clock=clock,
        delegation_store=delegations,
    )

    result = await manage_queues.expire_delegations(
        manage_queues.ExpireDelegationsCommand(
            tenant_id=owner.tenant_id,
            correlation_id="cid-expire",
        ),
        task_repo=tasks,
        delegation_store=delegations,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert result.restored == ()
    assert result.superseded == ()
    current = await tasks.get(owner.tenant_id, owned.id)
    assert current is not None
    assert current.assignee_user_id == delegate.user_id
    assert await delegations.list_active(owner.tenant_id) != []


async def test_expire_delegations_completed_task__drops_record_as_superseded() -> None:
    tasks = InMemoryTaskRepository()
    delegations = InMemoryDelegationStore()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()

    owner = _worker_user()
    delegate = _worker_user()
    owned = replace(_new_task(tenant_id=owner.tenant_id), assignee_user_id=owner.user_id)
    tasks.seed(owned)

    await manage_queues.delegate(
        manage_queues.DelegateCommand(
            tenant_id=owner.tenant_id,
            actor=owner,
            correlation_id="cid-delegate",
            delegate_user_id=delegate.user_id,
            task_ids=(owned.id,),
            until=NOW + timedelta(days=7),
        ),
        task_repo=tasks,
        audit=audit,
        outbox=outbox,
        clock=clock,
        delegation_store=delegations,
    )

    # given: the delegate finishes the work before the window passes —
    # there is nothing to hand back.
    stored = await tasks.get(owner.tenant_id, owned.id)
    assert stored is not None
    await tasks.update(replace(stored, status="completed"))
    clock.set_now(NOW + timedelta(days=8))

    # when
    result = await manage_queues.expire_delegations(
        manage_queues.ExpireDelegationsCommand(
            tenant_id=owner.tenant_id,
            correlation_id="cid-expire",
        ),
        task_repo=tasks,
        delegation_store=delegations,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # then: no restore, no expiry audit — the completion tells the story.
    assert result.restored == ()
    assert result.superseded == (owned.id,)
    current = await tasks.get(owner.tenant_id, owned.id)
    assert current is not None
    assert current.status == "completed"
    assert all(e.event_type != manage_queues.TASK_DELEGATION_EXPIRED for e in audit.events)
    assert await delegations.list_active(owner.tenant_id) == []


async def test_expire_delegations_reassigned_task__drops_record_as_superseded() -> None:
    tasks = InMemoryTaskRepository()
    delegations = InMemoryDelegationStore()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()

    owner = _worker_user()
    delegate = _worker_user()
    third = _worker_user()
    owned = replace(_new_task(tenant_id=owner.tenant_id), assignee_user_id=owner.user_id)
    tasks.seed(owned)

    await manage_queues.delegate(
        manage_queues.DelegateCommand(
            tenant_id=owner.tenant_id,
            actor=owner,
            correlation_id="cid-delegate",
            delegate_user_id=delegate.user_id,
            task_ids=(owned.id,),
            until=NOW + timedelta(days=7),
        ),
        task_repo=tasks,
        audit=audit,
        outbox=outbox,
        clock=clock,
        delegation_store=delegations,
    )

    # given: the delegate hands the task to someone else before expiry.
    stored = await tasks.get(owner.tenant_id, owned.id)
    assert stored is not None
    await tasks.update(replace(stored, assignee_user_id=third.user_id))
    clock.set_now(NOW + timedelta(days=8))

    # when
    result = await manage_queues.expire_delegations(
        manage_queues.ExpireDelegationsCommand(
            tenant_id=owner.tenant_id,
            correlation_id="cid-expire",
        ),
        task_repo=tasks,
        delegation_store=delegations,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # then: the third party keeps the task; the stale record is dropped.
    assert result.restored == ()
    assert result.superseded == (owned.id,)
    current = await tasks.get(owner.tenant_id, owned.id)
    assert current is not None
    assert current.assignee_user_id == third.user_id


async def test_expire_delegations__tenant_isolated() -> None:
    tasks = InMemoryTaskRepository()
    delegations = InMemoryDelegationStore()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()

    owner_b = _worker_user(TENANT_B)
    delegate_b = _worker_user(TENANT_B)
    owned_b = replace(_new_task(tenant_id=TENANT_B), assignee_user_id=owner_b.user_id)
    tasks.seed(owned_b)

    await manage_queues.delegate(
        manage_queues.DelegateCommand(
            tenant_id=TENANT_B,
            actor=owner_b,
            correlation_id="cid-delegate",
            delegate_user_id=delegate_b.user_id,
            task_ids=(owned_b.id,),
            until=NOW + timedelta(days=7),
        ),
        task_repo=tasks,
        audit=audit,
        outbox=outbox,
        clock=clock,
        delegation_store=delegations,
    )
    clock.set_now(NOW + timedelta(days=8))

    # when: the sweep runs for tenant A (which has no delegations)
    result = await manage_queues.expire_delegations(
        manage_queues.ExpireDelegationsCommand(
            tenant_id=TENANT_A,
            correlation_id="cid-expire",
        ),
        task_repo=tasks,
        delegation_store=delegations,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # then: tenant B is untouched.
    assert result.restored == ()
    assert result.superseded == ()
    current = await tasks.get(TENANT_B, owned_b.id)
    assert current is not None
    assert current.assignee_user_id == delegate_b.user_id
    assert len(await delegations.list_active(TENANT_B)) == 1


async def test_delegate_without_store__still_hands_off_without_record() -> None:
    # Backward compatibility: callers that have not wired a
    # delegation store get the audit-row-only window (pre-expiry
    # behavior); the handoff itself is unaffected.
    tasks = InMemoryTaskRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()

    owner = _worker_user()
    delegate = _worker_user()
    owned = replace(_new_task(tenant_id=owner.tenant_id), assignee_user_id=owner.user_id)
    tasks.seed(owned)

    delegated = await manage_queues.delegate(
        manage_queues.DelegateCommand(
            tenant_id=owner.tenant_id,
            actor=owner,
            correlation_id="cid-delegate",
            delegate_user_id=delegate.user_id,
            task_ids=(owned.id,),
            until=NOW + timedelta(days=7),
        ),
        task_repo=tasks,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert len(delegated) == 1
    current = await tasks.get(owner.tenant_id, owned.id)
    assert current is not None
    assert current.assignee_user_id == delegate.user_id


# ---------------------------------------------------------------------------
# 6. Human-driven cycles allowed, automatic cycles rejected (validator pin)
# ---------------------------------------------------------------------------


def _cycle_version(*, human_driven_cycle_edge: bool) -> WorkflowVersion:
    return WorkflowVersion.draft(
        tenant_id=TENANT_A,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="in_review", label="In Review"),
            WorkflowState(key="needs_info", label="Needs Info"),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="submit",
                from_state="intake",
                to_state="in_review",
                human_driven=True,
            ),
            WorkflowTransition(
                key="request_info",
                from_state="in_review",
                to_state="needs_info",
                human_driven=human_driven_cycle_edge,
            ),
            WorkflowTransition(
                key="re_review",
                from_state="needs_info",
                to_state="in_review",
                human_driven=human_driven_cycle_edge,
            ),
            WorkflowTransition(
                key="close",
                from_state="in_review",
                to_state="closed",
                human_driven=True,
            ),
        ),
        now=None,
    )


def test_workflow_human_driven_cycle__allowed() -> None:
    # The canonical reviewer loop survives publish validation: a human
    # breaks the cycle, so it cannot run away on its own.
    validate_definition(_cycle_version(human_driven_cycle_edge=True))


def test_workflow_automatic_cycle__rejected() -> None:
    # The same shape with every cycle edge automatic is a runaway loop
    # and must fail publish validation with a named blocker.
    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(_cycle_version(human_driven_cycle_edge=False))
    assert "cycle" in str(excinfo.value).lower()


__all__: list[str] = []
