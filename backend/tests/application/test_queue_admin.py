"""Tests for queue admin + worker claim/release flow (P02-US3, US9 / FM-016).

Covers:

- Deny-by-default: a non-admin actor cannot define / reconfigure /
  retire a queue (Forbidden).
- An admin can define a queue with ``manual`` or ``round_robin``
  policy and reconfigure the members; the aggregate enforces
  invariants (no duplicate members, slug grammar, RR requires
  members).
- Worker claim moves a task's ``assignee_user_id`` from queue to
  user; release returns it; bulk-handle processes a batch in one
  audit row.
- ``round_robin`` cycles through members on successive claims; a
  no-op claim (empty queue) leaves the cursor alone.
- Tenant isolation: a queue in tenant A is invisible from tenant
  B; a claim in tenant A never moves a tenant B task.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per
AGENTS.md §4.R5.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.authorization import Action, Policy, Resource
from app.application.queues import manage_queues
from app.deps import UserContext
from app.domain.errors import Conflict, InvariantViolation, NotFound
from app.domain.queue import AssignmentPolicy, Queue

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes — local, mirror ``tests/application/_workflow_fakes.py`` style.
# ---------------------------------------------------------------------------


class FixedClock:
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or NOW

    def now(self) -> datetime:
        return self._now


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
        if (queue.tenant_id, queue.key) in self._queues_by_key:
            raise Conflict(f"queue {queue.key!r} already exists in tenant")
        self._queues[(queue.tenant_id, queue.id)] = queue
        self._queues_by_key[(queue.tenant_id, queue.key)] = queue

    async def update(self, queue: Queue) -> None:
        existing = self._queues.get((queue.tenant_id, queue.id))
        if existing is None:
            return
        if existing.version_token != queue.version_token - 1 and (
            existing.version_token >= queue.version_token
        ):
            raise Conflict(f"queue {queue.id} version_token stale")
        self._queues[(queue.tenant_id, queue.id)] = queue
        self._queues_by_key[(queue.tenant_id, queue.key)] = queue

    async def delete(self, tenant_id: UUID, queue_id: UUID) -> None:
        q = self._queues.pop((tenant_id, queue_id), None)
        if q is not None:
            self._queues_by_key.pop((tenant_id, q.key), None)


@dataclass
class FakeTask:
    """In-memory stand-in for ``Task``."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    type: str
    title: str
    status: str = "open"
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

    async def get(self, tenant_id: UUID, task_id: UUID) -> FakeTask | None:
        return self._tasks.get((tenant_id, task_id))

    async def update(self, task: FakeTask) -> None:
        existing = self._tasks.get((task.tenant_id, task.id))
        if existing is None:
            return
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
            key=lambda t: (t.due_at is None, t.due_at or NOW, t.created_at),
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


class InMemoryCaseLookup:
    def __init__(self) -> None:
        self._cases: dict[tuple[UUID, UUID], dict[str, Any]] = {}

    def seed(self, tenant_id: UUID, case_id: UUID) -> None:
        self._cases[(tenant_id, case_id)] = {"id": case_id, "tenant_id": tenant_id}

    async def get(self, tenant_id: UUID, case_id: UUID) -> dict[str, Any] | None:
        return self._cases.get((tenant_id, case_id))


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


def _new_case_id() -> UUID:
    return uuid4()


def _new_task(
    *,
    tenant_id: UUID = TENANT_A,
    case_id: UUID | None = None,
    queue_key: str = "review_queue",
    due_at: datetime | None = None,
) -> FakeTask:
    return FakeTask(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id or _new_case_id(),
        type="review",
        title="Review intake",
        assignee_queue_id=queue_key,
        due_at=due_at,
    )


# ---------------------------------------------------------------------------
# 1. Define queue
# ---------------------------------------------------------------------------


async def test_define_queue_non_admin_is_forbidden() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    actor = _worker_user()

    with pytest.raises(InvariantViolation):
        await manage_queues.define_queue(
            manage_queues.DefineQueueCommand(
                tenant_id=actor.tenant_id,
                actor=actor,
                correlation_id="cid",
                key="review_queue",
                name="Review queue",
            ),
            queue_repo=repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    assert await repo.list_keys(actor.tenant_id) == []
    assert audit.events == []
    assert outbox.events == []


async def test_define_queue_admin_persists_and_audits() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    actor = _admin_user()

    q = await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=actor.tenant_id,
            actor=actor,
            correlation_id="cid-define",
            key="review_queue",
            name="Review queue",
            description="default review",
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert q.key == "review_queue"
    assert q.assignment_policy is AssignmentPolicy.MANUAL
    assert q.member_user_ids == ()
    assert len(audit.events) == 1
    assert audit.events[0].event_type == manage_queues.QUEUE_DEFINED
    assert outbox.events[0].event_type == manage_queues.QUEUE_DEFINED


async def test_define_queue_round_robin_requires_members() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    actor = _admin_user()

    with pytest.raises(InvariantViolation):
        await manage_queues.define_queue(
            manage_queues.DefineQueueCommand(
                tenant_id=actor.tenant_id,
                actor=actor,
                correlation_id="cid",
                key="rr_queue",
                name="Round robin",
                assignment_policy=AssignmentPolicy.ROUND_ROBIN,
                member_user_ids=(),
            ),
            queue_repo=repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    assert await repo.list_keys(actor.tenant_id) == []


async def test_define_queue_rejects_bad_key() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    actor = _admin_user()

    with pytest.raises(InvariantViolation):
        await manage_queues.define_queue(
            manage_queues.DefineQueueCommand(
                tenant_id=actor.tenant_id,
                actor=actor,
                correlation_id="cid",
                key="Not Allowed!",
                name="Bad key",
            ),
            queue_repo=repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


async def test_define_queue_duplicate_key_raises_conflict() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    actor = _admin_user()

    cmd = manage_queues.DefineQueueCommand(
        tenant_id=actor.tenant_id,
        actor=actor,
        correlation_id="cid",
        key="review_queue",
        name="Review",
    )
    await manage_queues.define_queue(cmd, queue_repo=repo, audit=audit, outbox=outbox, clock=clock)

    with pytest.raises(Conflict):
        await manage_queues.define_queue(
            cmd, queue_repo=repo, audit=audit, outbox=outbox, clock=clock
        )


# ---------------------------------------------------------------------------
# 2. Tenant isolation
# ---------------------------------------------------------------------------


async def test_queue_tenant_isolation_get_by_key_returns_none_across_tenants() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()

    await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=TENANT_A,
            actor=_admin_user(TENANT_A),
            correlation_id="cid",
            key="shared_key",
            name="In A",
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    cross = await repo.get_by_key(TENANT_B, "shared_key")
    assert cross is None
    # Same key string, different tenant — surfaces as invisible
    # (Constitution V), never as 404 with the wrong tenant's row.
    assert (await repo.list_keys(TENANT_A)) == ["shared_key"]
    assert (await repo.list_keys(TENANT_B)) == []


# ---------------------------------------------------------------------------
# 3. Claim / release
# ---------------------------------------------------------------------------


async def test_claim_next_manual_moves_assignee_from_queue_to_user() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()
    cases = InMemoryCaseLookup()

    admin = _admin_user()
    worker = _worker_user()

    q = await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=worker.tenant_id,
            actor=admin,
            correlation_id="cid-define",
            key="review_queue",
            name="Review",
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    case_id = _new_case_id()
    cases.seed(worker.tenant_id, case_id)
    task = _new_task(tenant_id=worker.tenant_id, case_id=case_id)
    tasks.seed(task)

    result = await manage_queues.claim_next(
        manage_queues.ClaimNextCommand(
            tenant_id=worker.tenant_id,
            actor=worker,
            correlation_id="cid-claim",
            queue_key=q.key,
        ),
        queue_repo=repo,
        task_repo=tasks,
        case_lookup=cases,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert result is not None
    assert result.task.assignee_user_id == worker.user_id
    assert result.task.assignee_queue_id is None
    assert result.task.version_token == 2

    # Audit + outbox rows recorded.
    claim_events = [e for e in audit.events if e.event_type == manage_queues.TASK_CLAIMED]
    assert len(claim_events) == 1
    assert claim_events[0].payload["queue_key"] == "review_queue"
    assert any(e.event_type == manage_queues.TASK_CLAIMED for e in outbox.events)


async def test_claim_next_empty_queue_returns_none() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()
    cases = InMemoryCaseLookup()

    admin = _admin_user()
    worker = _worker_user()
    await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=worker.tenant_id,
            actor=admin,
            correlation_id="cid",
            key="review_queue",
            name="Review",
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    result = await manage_queues.claim_next(
        manage_queues.ClaimNextCommand(
            tenant_id=worker.tenant_id,
            actor=worker,
            correlation_id="cid-claim",
            queue_key="review_queue",
        ),
        queue_repo=repo,
        task_repo=tasks,
        case_lookup=cases,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert result is None
    # No claim event fired (nothing to claim).
    assert all(e.event_type != manage_queues.TASK_CLAIMED for e in audit.events)


async def test_release_task_returns_assignee_to_queue() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()

    admin = _admin_user()
    worker = _worker_user()
    await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=worker.tenant_id,
            actor=admin,
            correlation_id="cid",
            key="review_queue",
            name="Review",
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    case_id = _new_case_id()
    task = _new_task(tenant_id=worker.tenant_id, case_id=case_id)
    task = replace(task, assignee_user_id=worker.user_id)
    tasks.seed(task)

    released = await manage_queues.release_task(
        manage_queues.ReleaseTaskCommand(
            tenant_id=worker.tenant_id,
            actor=worker,
            correlation_id="cid-release",
            task_id=task.id,
            reason="scope changed",
        ),
        task_repo=tasks,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert released.assignee_user_id is None
    assert released.assignee_queue_id == "review_queue"
    assert any(e.event_type == manage_queues.TASK_RELEASED for e in audit.events)


async def test_release_task_by_non_assignee_is_conflict() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()

    admin = _admin_user()
    worker = _worker_user()
    other = _worker_user()
    await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=worker.tenant_id,
            actor=admin,
            correlation_id="cid",
            key="review_queue",
            name="Review",
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    case_id = _new_case_id()
    task = _new_task(tenant_id=worker.tenant_id, case_id=case_id)
    task = replace(task, assignee_user_id=worker.user_id)
    tasks.seed(task)

    with pytest.raises(Conflict):
        await manage_queues.release_task(
            manage_queues.ReleaseTaskCommand(
                tenant_id=other.tenant_id,
                actor=other,
                correlation_id="cid-release",
                task_id=task.id,
            ),
            task_repo=tasks,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 4. Round-robin
# ---------------------------------------------------------------------------


async def test_round_robin_cycles_assignees_on_successive_claims() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()
    cases = InMemoryCaseLookup()

    member_a = _worker_user()
    member_b = _worker_user()
    member_c = _worker_user()
    members = (member_a, member_b, member_c)

    q = Queue.create(
        tenant_id=TENANT_A,
        key="rr_queue",
        name="Round robin",
        assignment_policy=AssignmentPolicy.ROUND_ROBIN,
        member_user_ids=(member_a.user_id, member_b.user_id, member_c.user_id),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await repo.add(q)

    # Seed 3 tasks, one per claim; expect cursor to cycle members.
    for _ in range(3):
        case_id = _new_case_id()
        cases.seed(TENANT_A, case_id)
        tasks.seed(_new_task(tenant_id=TENANT_A, case_id=case_id, queue_key="rr_queue"))

    seen: list[UUID] = []
    for _ in range(3):
        claimant = members[len(seen)]
        result = await manage_queues.claim_next(
            manage_queues.ClaimNextCommand(
                tenant_id=TENANT_A,
                actor=claimant,
                correlation_id="cid-claim",
                queue_key="rr_queue",
            ),
            queue_repo=repo,
            task_repo=tasks,
            case_lookup=cases,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
        assert result is not None
        seen.append(result.task.assignee_user_id)

    # Round-robin advances in declared order (member_a, member_b,
    # member_c, then wraps).
    assert seen == [member_a.user_id, member_b.user_id, member_c.user_id]


async def test_round_robin_advances_only_on_successful_claim() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()
    cases = InMemoryCaseLookup()

    member_a = _worker_user()
    member_b = _worker_user()
    q = Queue.create(
        tenant_id=TENANT_A,
        key="rr_queue",
        name="RR",
        assignment_policy=AssignmentPolicy.ROUND_ROBIN,
        member_user_ids=(member_a.user_id, member_b.user_id),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    await repo.add(q)

    # First call: empty queue -> cursor unchanged.
    result = await manage_queues.claim_next(
        manage_queues.ClaimNextCommand(
            tenant_id=TENANT_A,
            actor=member_a,
            correlation_id="cid-empty",
            queue_key="rr_queue",
        ),
        queue_repo=repo,
        task_repo=tasks,
        case_lookup=cases,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert result is None
    current = await repo.get_by_key(TENANT_A, "rr_queue")
    assert current is not None
    assert current.rr_cursor == 0

    # Seed a task, claim it; cursor should advance by 1.
    cases.seed(TENANT_A, _new_case_id())
    tasks.seed(_new_task(tenant_id=TENANT_A, queue_key="rr_queue"))
    result = await manage_queues.claim_next(
        manage_queues.ClaimNextCommand(
            tenant_id=TENANT_A,
            actor=member_a,
            correlation_id="cid-claim",
            queue_key="rr_queue",
        ),
        queue_repo=repo,
        task_repo=tasks,
        case_lookup=cases,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert result is not None
    current = await repo.get_by_key(TENANT_A, "rr_queue")
    assert current is not None
    assert current.rr_cursor == 1


# ---------------------------------------------------------------------------
# 5. Bulk handle (10 tasks)
# ---------------------------------------------------------------------------


async def test_bulk_handle_claim_processes_ten_tasks_in_one_audit_row() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()

    admin = _admin_user()
    worker = _worker_user()
    await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=worker.tenant_id,
            actor=admin,
            correlation_id="cid",
            key="bulk_queue",
            name="Bulk",
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    seeded: list[FakeTask] = []
    for _ in range(10):
        case_id = _new_case_id()
        seeded.append(
            tasks.seed(
                _new_task(tenant_id=worker.tenant_id, case_id=case_id, queue_key="bulk_queue")
            )
        )

    result = await manage_queues.bulk_handle(
        manage_queues.BulkHandleCommand(
            tenant_id=worker.tenant_id,
            actor=worker,
            correlation_id="cid-bulk",
            action="claim",
            task_ids=tuple(t.id for t in seeded),
            queue_key="bulk_queue",
        ),
        queue_repo=repo,
        task_repo=tasks,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert len(result.handled) == 10
    assert result.skipped == ()
    # Exactly one bulk audit row; per-task eventing is not duplicated.
    bulk_events = [e for e in audit.events if e.event_type == manage_queues.TASK_BULK_HANDLED]
    assert len(bulk_events) == 1
    assert bulk_events[0].payload["handled_count"] == 10

    # Every task now belongs to the worker.
    for t in seeded:
        current = await tasks.get(worker.tenant_id, t.id)
        assert current is not None
        assert current.assignee_user_id == worker.user_id
        assert current.assignee_queue_id is None


async def test_bulk_handle_skips_missing_ids_does_not_abort() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()

    admin = _admin_user()
    worker = _worker_user()
    await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=worker.tenant_id,
            actor=admin,
            correlation_id="cid",
            key="bulk_queue",
            name="Bulk",
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    existing = _new_task(tenant_id=worker.tenant_id, queue_key="bulk_queue")
    tasks.seed(existing)
    missing = uuid4()

    result = await manage_queues.bulk_handle(
        manage_queues.BulkHandleCommand(
            tenant_id=worker.tenant_id,
            actor=worker,
            correlation_id="cid-bulk",
            action="claim",
            task_ids=(existing.id, missing),
            queue_key="bulk_queue",
        ),
        queue_repo=repo,
        task_repo=tasks,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert len(result.handled) == 1
    assert result.skipped == (missing,)


async def test_bulk_handle_invalid_action_raises() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()

    worker = _worker_user()
    with pytest.raises(InvariantViolation):
        await manage_queues.bulk_handle(
            manage_queues.BulkHandleCommand(
                tenant_id=worker.tenant_id,
                actor=worker,
                correlation_id="cid",
                action="bogus",
                task_ids=(uuid4(),),
            ),
            queue_repo=repo,
            task_repo=tasks,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 6. Cross-tenant isolation
# ---------------------------------------------------------------------------


async def test_claim_does_not_move_cross_tenant_tasks() -> None:
    repo = InMemoryQueueRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()
    cases = InMemoryCaseLookup()

    admin_a = _admin_user(TENANT_A)
    worker_a = _worker_user(TENANT_A)
    worker_b = _worker_user(TENANT_B)
    await manage_queues.define_queue(
        manage_queues.DefineQueueCommand(
            tenant_id=TENANT_A,
            actor=admin_a,
            correlation_id="cid",
            key="review_queue",
            name="Review",
        ),
        queue_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    tenant_b_task = _new_task(tenant_id=TENANT_B, queue_key="review_queue")
    tasks.seed(tenant_b_task)
    cases.seed(TENANT_B, tenant_b_task.case_id)

    # Worker in tenant A claiming their own queue must NOT pick up
    # a tenant B task.
    result = await manage_queues.claim_next(
        manage_queues.ClaimNextCommand(
            tenant_id=worker_a.tenant_id,
            actor=worker_a,
            correlation_id="cid-claim",
            queue_key="review_queue",
        ),
        queue_repo=repo,
        task_repo=tasks,
        case_lookup=cases,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert result is None

    # Worker B has the same key but tenant B has no queue configured;
    # claim surfaces as NotFound, never as "claimed the A-tenant row".
    with pytest.raises(NotFound):
        await manage_queues.claim_next(
            manage_queues.ClaimNextCommand(
                tenant_id=worker_b.tenant_id,
                actor=worker_b,
                correlation_id="cid-claim",
                queue_key="review_queue",
            ),
            queue_repo=repo,
            task_repo=tasks,
            case_lookup=cases,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    # The tenant B task remains untouched.
    untouched = await tasks.get(TENANT_B, tenant_b_task.id)
    assert untouched is not None
    assert untouched.assignee_user_id is None
    assert untouched.assignee_queue_id == "review_queue"


# ---------------------------------------------------------------------------
# 7. Delegation (P02-US9 / FM-016)
# ---------------------------------------------------------------------------


async def test_delegate_hands_open_tasks_to_another_user_for_window() -> None:
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()

    actor = _worker_user()
    delegate = _worker_user()

    case_id = _new_case_id()
    owned = replace(
        _new_task(tenant_id=actor.tenant_id, case_id=case_id),
        assignee_user_id=actor.user_id,
    )
    tasks.seed(owned)

    until = clock.now() + timedelta(days=7)
    delegated = await manage_queues.delegate(
        manage_queues.DelegateCommand(
            tenant_id=actor.tenant_id,
            actor=actor,
            correlation_id="cid-delegate",
            delegate_user_id=delegate.user_id,
            task_ids=(owned.id,),
            until=until,
        ),
        task_repo=tasks,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert len(delegated) == 1
    moved = await tasks.get(actor.tenant_id, owned.id)
    assert moved is not None
    assert moved.assignee_user_id == delegate.user_id
    delegation_events = [e for e in audit.events if e.event_type == manage_queues.TASK_DELEGATED]
    assert len(delegation_events) == 1
    assert delegation_events[0].payload["delegate_user_id"] == str(delegate.user_id)
    assert delegation_events[0].payload["until"] == until.isoformat()


async def test_delegate_to_self_is_rejected() -> None:
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()

    actor = _worker_user()
    with pytest.raises(InvariantViolation):
        await manage_queues.delegate(
            manage_queues.DelegateCommand(
                tenant_id=actor.tenant_id,
                actor=actor,
                correlation_id="cid-delegate",
                delegate_user_id=actor.user_id,
                task_ids=(uuid4(),),
                until=clock.now() + timedelta(days=1),
            ),
            task_repo=tasks,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


async def test_delegate_past_window_is_rejected() -> None:
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    clock = FixedClock()
    tasks = InMemoryTaskRepository()

    actor = _worker_user()
    delegate = _worker_user()
    with pytest.raises(InvariantViolation):
        await manage_queues.delegate(
            manage_queues.DelegateCommand(
                tenant_id=actor.tenant_id,
                actor=actor,
                correlation_id="cid-delegate",
                delegate_user_id=delegate.user_id,
                task_ids=(uuid4(),),
                until=clock.now() - timedelta(seconds=1),
            ),
            task_repo=tasks,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 8. Policy sanity
# ---------------------------------------------------------------------------


async def test_policy_authorize_admin_action_without_role_denies() -> None:
    actor = _worker_user()
    decision = await Policy().evaluate(
        Action.ADMIN_TENANT_CONFIG,
        actor,
        Resource(tenant_id=actor.tenant_id, kind="queue"),
    )
    assert decision.allowed is False


__all__ = []
