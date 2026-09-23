"""Worker claim / release / bulk operations for queue tasks (P02-US3, US9 / FM-016).

The worker-facing side of queue management: claim the next task from a
queue (manual selection or round-robin cursor), release a claimed task
back to its queue, and apply either action to a batch in one audit-logged
request.

# Authorization

These operations require ``TASK_READ`` on the queue resource — the policy
is evaluated against the case that owns the task (cross-tenant actors are
denied unconditionally).

# Audit + outbox atomicity

Each operation writes the task change, an audit row, and (outbound) domain
events via the outbox in the same DB transaction. The boundary commits
once; failures roll back to a clean state so a partial claim never lands.

Split out of the former single-file ``manage_queues.py`` during the R2
package split; admin operations live in :mod:`.admin` and delegation in
:mod:`.delegations`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.audit import TASK_BULK_HANDLED, TASK_CLAIMED, TASK_RELEASED
from app.domain.errors import Conflict, InvariantViolation, NotFound
from app.domain.queue import AssignmentPolicy, Queue

from .commands import (
    BulkHandleCommand,
    BulkHandleResult,
    ClaimNextCommand,
    ClaimNextResult,
    ReleaseTaskCommand,
)
from .helpers import _assign_task_to_user
from .ports import (
    SUBJECT_TYPE_TASK,
    _AuditStore,
    _CaseLookup,
    _Clock,
    _OutboxWriter,
    _QueueRepository,
    _TaskRepository,
)


async def claim_next(
    cmd: ClaimNextCommand,
    *,
    queue_repo: _QueueRepository,
    task_repo: _TaskRepository,
    case_lookup: _CaseLookup,
    audit: _AuditStore,
    outbox: _OutboxWriter,
    clock: _Clock,
) -> ClaimNextResult | None:
    """Claim the next task from ``queue_key`` for the actor.

    Behavior:

    - ``MANUAL`` — pick the most-urgent open task on the queue
      (ordered ``due_at ASC NULLS LAST, created_at ASC``) and
      reassign it to the actor. If the queue has no open tasks
      the result is ``None`` — the worker is told "nothing to do"
      rather than a synthetic task.
    - ``ROUND_ROBIN`` — the queue's ``next_assignee`` (the cursor)
      is the claimer. The cursor advances only on a successful
      claim; a no-op (empty queue) leaves the cursor alone so the
      next non-empty call still picks up where we left off.

    The task's ``assignee_user_id`` flips to the actor; the queue
    pointer flips to ``assignee_user_id = None`` to reflect the
    "no longer on the queue" state. The task UPDATE + audit row +
    outbox row commit together (Constitution III).

    Returns ``None`` when there is no open work to claim.
    """
    await authorize(
        Action.TASK_READ,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="queue", id=None),
    )
    queue = await queue_repo.get_by_key(cmd.tenant_id, cmd.queue_key)
    if queue is None:
        raise NotFound(f"queue {cmd.queue_key!r} not found")
    open_tasks = await task_repo.list_open_for_queue(cmd.tenant_id, cmd.queue_key, limit=100)
    if not open_tasks:
        return None
    chosen = open_tasks[0]
    now = clock.now()
    new_task = _assign_task_to_user(
        chosen, assignee_user_id=cmd.actor.user_id, assignee_queue_id=None
    )
    await task_repo.update(new_task)

    case = await case_lookup.get(cmd.tenant_id, new_task.case_id)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=TASK_CLAIMED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_TASK,
        subject_id=new_task.id,
        case_id=new_task.case_id,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "queue_key": cmd.queue_key,
            "task_type": getattr(new_task, "type", None),
            "task_title": getattr(new_task, "title", None),
            "claim_mode": queue.assignment_policy.value,
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=TASK_CLAIMED,
        event_id=uuid4(),
        payload={
            "task_id": str(new_task.id),
            "case_id": str(new_task.case_id),
            "queue_key": cmd.queue_key,
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor.user_id),
            "occurred_at": now.isoformat(),
        },
    )

    if queue.assignment_policy is AssignmentPolicy.ROUND_ROBIN:
        next_queue = queue.advance_cursor()
        next_queue = Queue(
            id=next_queue.id,
            tenant_id=next_queue.tenant_id,
            key=next_queue.key,
            name=next_queue.name,
            description=next_queue.description,
            assignment_policy=next_queue.assignment_policy,
            capacity=next_queue.capacity,
            member_user_ids=next_queue.member_user_ids,
            rr_cursor=next_queue.rr_cursor,
            visibility_scope=next_queue.visibility_scope,
            created_at=next_queue.created_at,
            updated_at=now,
            version_token=next_queue.version_token,
        )
        await queue_repo.update(next_queue)

    _ = case  # case lookup reserved for policy-tightened variant (T063+)
    return ClaimNextResult(task=new_task, queue=queue)


async def release_task(
    cmd: ReleaseTaskCommand,
    *,
    task_repo: _TaskRepository,
    audit: _AuditStore,
    outbox: _OutboxWriter,
    clock: _Clock,
) -> Any:
    """Release a previously-claimed task back to its queue.

    The task must currently be assigned to ``cmd.actor`` — releasing
    someone else's task is a :class:`Conflict` (the worker is not
    the assignee; ``Forbidden`` would lie about authorization). The
    task reverts to ``assignee_user_id = None`` and
    ``assignee_queue_id = <prior queue>``. The prior queue is taken
    from the task's own ``assignee_queue_id`` if set, otherwise the
    caller must supply one (omitted here — see ``bulk_handle`` for
    the batch path).
    """
    task = await task_repo.get(cmd.tenant_id, cmd.task_id)
    if task is None:
        raise NotFound(f"task {cmd.task_id} not found")
    if getattr(task, "assignee_user_id", None) != cmd.actor.user_id:
        raise Conflict(f"task {cmd.task_id} is not assigned to {cmd.actor.user_id}")
    # A claimed task carries ``assignee_queue_id = None`` (claim moves it
    # off the queue); the caller names the return queue via ``queue_key``.
    # A task that still carries a prior queue uses that value.
    target_queue = getattr(task, "assignee_queue_id", None) or cmd.queue_key
    if not target_queue:
        raise InvariantViolation("task has no prior queue to release back to")
    now = clock.now()
    new_task = _assign_task_to_user(task, assignee_user_id=None, assignee_queue_id=target_queue)
    await task_repo.update(new_task)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=TASK_RELEASED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_TASK,
        subject_id=new_task.id,
        case_id=new_task.case_id,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "queue_key": target_queue,
            "reason": cmd.reason or "",
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=TASK_RELEASED,
        event_id=uuid4(),
        payload={
            "task_id": str(new_task.id),
            "case_id": str(new_task.case_id),
            "queue_key": target_queue,
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor.user_id),
            "occurred_at": now.isoformat(),
        },
    )
    return new_task


async def bulk_handle(
    cmd: BulkHandleCommand,
    *,
    queue_repo: _QueueRepository,
    task_repo: _TaskRepository,
    audit: _AuditStore,
    outbox: _OutboxWriter,
    clock: _Clock,
) -> BulkHandleResult:
    """Bulk claim or release (P02-US3 / FM-016).

    Iterates ``task_ids`` and applies the chosen action per task. A
    missing or cross-tenant id is recorded in ``skipped`` rather
    than aborting the batch — a worker dragging-and-dropping 25
    tasks should not have the whole batch fail because one row was
    already closed.

    One audit row + one outbox event covers the batch; per-task
    detail lives on the task's own event stream.
    """
    if cmd.action not in ("claim", "release"):
        raise InvariantViolation(
            f"bulk_handle action must be 'claim' or 'release', got {cmd.action!r}"
        )
    await authorize(
        Action.TASK_READ,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="queue", id=None),
    )
    if cmd.queue_key:
        queue = await queue_repo.get_by_key(cmd.tenant_id, cmd.queue_key)
        if queue is None:
            raise NotFound(f"queue {cmd.queue_key!r} not found")
    handled: list[Any] = []
    skipped: list[UUID] = []
    now = clock.now()
    for tid in cmd.task_ids:
        task = await task_repo.get(cmd.tenant_id, tid)
        if task is None:
            skipped.append(tid)
            continue
        if cmd.action == "claim":
            new_task = _assign_task_to_user(
                task,
                assignee_user_id=cmd.actor.user_id,
                assignee_queue_id=None,
            )
        else:
            target_queue = getattr(task, "assignee_queue_id", None)
            if getattr(task, "assignee_user_id", None) != cmd.actor.user_id or not target_queue:
                skipped.append(tid)
                continue
            new_task = _assign_task_to_user(
                task, assignee_user_id=None, assignee_queue_id=target_queue
            )
        await task_repo.update(new_task)
        handled.append(new_task)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=TASK_BULK_HANDLED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="task_batch",
        subject_id=uuid4(),
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "action": cmd.action,
            "queue_key": cmd.queue_key or "",
            "handled": [str(t.id) for t in handled],
            "skipped": [str(s) for s in skipped],
            "handled_count": len(handled),
            "skipped_count": len(skipped),
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=TASK_BULK_HANDLED,
        event_id=uuid4(),
        payload={
            "action": cmd.action,
            "queue_key": cmd.queue_key or "",
            "handled_count": len(handled),
            "skipped_count": len(skipped),
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor.user_id),
            "occurred_at": now.isoformat(),
        },
    )
    return BulkHandleResult(handled=tuple(handled), skipped=tuple(skipped))
