"""Delegation use cases: hand off open tasks and expire the window (P02-US9 / FM-016).

A worker may delegate their open tasks to another identity for a bounded
period (:func:`delegate`); a system sweep restores the prior owner once the
window passes (:func:`expire_delegations`). Tasks the delegate already
completed or handed off are left alone — the delegation record is dropped
as superseded.

# Authorization

``delegate`` requires ``TASK_READ`` on the delegate's user resource;
``expire_delegations`` is system-initiated (tenant scope is the boundary,
same as the SLA scanner) with no actor auth.

# Audit + outbox atomicity

Each mutation writes the task change, an audit row, and an outbox event in
the same DB transaction.

Split out of the former single-file ``manage_queues.py`` during the R2
package split; admin operations live in :mod:`.admin` and claim/release in
:mod:`.claims`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.audit import TASK_DELEGATED, TASK_DELEGATION_EXPIRED
from app.domain.errors import InvariantViolation
from app.domain.queue import Delegation

from .commands import (
    DelegateCommand,
    ExpireDelegationsCommand,
    ExpireDelegationsResult,
)
from .helpers import _assign_task_to_user
from .ports import (
    SUBJECT_TYPE_TASK,
    _AuditStore,
    _Clock,
    _DelegationStore,
    _OutboxWriter,
    _TaskRepository,
)


async def delegate(
    cmd: DelegateCommand,
    *,
    task_repo: _TaskRepository,
    audit: _AuditStore,
    outbox: _OutboxWriter,
    clock: _Clock,
    delegation_store: _DelegationStore | None = None,
) -> tuple[Any, ...]:
    """Delegate the actor's open tasks to ``delegate_user_id`` (P02-US9).

    The bounded period (``until``) is recorded on the audit row and,
    when ``delegation_store`` is wired, as a :class:`Delegation`
    record per task so :func:`expire_delegations` can restore the
    prior owner once the window passes. The bound is exclusive; a
    delegation past ``now > until`` is a 409 so a worker cannot
    silently hand off work they no longer own.
    """
    if cmd.until <= clock.now():
        raise InvariantViolation("delegation window must be in the future")
    if cmd.delegate_user_id == cmd.actor.user_id:
        raise InvariantViolation("cannot delegate to self")
    await authorize(
        Action.TASK_READ,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="user", id=cmd.delegate_user_id),
    )
    delegated: list[Any] = []
    skipped: list[UUID] = []
    now = clock.now()
    for tid in cmd.task_ids:
        task = await task_repo.get(cmd.tenant_id, tid)
        if task is None or getattr(task, "assignee_user_id", None) != cmd.actor.user_id:
            skipped.append(tid)
            continue
        new_task = _assign_task_to_user(
            task,
            assignee_user_id=cmd.delegate_user_id,
            assignee_queue_id=None,
        )
        await task_repo.update(new_task)
        delegated.append(new_task)
        if delegation_store is not None:
            await delegation_store.record(
                Delegation.create(
                    tenant_id=cmd.tenant_id,
                    task_id=new_task.id,
                    case_id=new_task.case_id,
                    from_user_id=cmd.actor.user_id,
                    to_user_id=cmd.delegate_user_id,
                    delegated_at=now,
                    until=cmd.until,
                )
            )
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=TASK_DELEGATED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type="task_batch",
        subject_id=uuid4(),
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "delegate_user_id": str(cmd.delegate_user_id),
            "delegated": [str(t.id) for t in delegated],
            "skipped": [str(s) for s in skipped],
            "until": cmd.until.isoformat(),
            "window_seconds": int((cmd.until - now).total_seconds()),
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=TASK_DELEGATED,
        event_id=uuid4(),
        payload={
            "delegate_user_id": str(cmd.delegate_user_id),
            "delegated_count": len(delegated),
            "until": cmd.until.isoformat(),
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor.user_id),
            "occurred_at": now.isoformat(),
        },
    )
    _ = skipped  # payload already records the skipped list
    return tuple(delegated)


async def expire_delegations(
    cmd: ExpireDelegationsCommand,
    *,
    task_repo: _TaskRepository,
    delegation_store: _DelegationStore,
    audit: _AuditStore,
    outbox: _OutboxWriter,
    clock: _Clock,
) -> ExpireDelegationsResult:
    """Restore prior owners whose delegation windows have passed (P02-US9).

    System-initiated sweep (no actor auth — tenant scope is the
    boundary, same as the SLA scanner). For every active delegation
    with ``now > until``:

    - task still ``open`` and still held by the delegate → reassign
      to ``from_user_id`` (the prior owner), bump the version token,
      write a ``TaskDelegationExpired`` audit row + outbox event, and
      drop the record. This is the expiry-restore acceptance: the
      owner returns without any human action.
    - anything else (completed, cancelled, reassigned away, or the
      row vanished cross-tenant) → drop the record as superseded.
      No restore, no audit: the task already tells its own story.

    Unexpired records are left untouched so a later sweep (or a
    later ``now`` in tests) still sees them.
    """
    now = clock.now()
    restored: list[Any] = []
    superseded: list[UUID] = []
    for record in await delegation_store.list_active(cmd.tenant_id):
        if not record.is_expired(now=now):
            continue
        task = await task_repo.get(cmd.tenant_id, record.task_id)
        if (
            task is not None
            and getattr(task, "status", None) == "open"
            and getattr(task, "assignee_user_id", None) == record.to_user_id
        ):
            new_task = _assign_task_to_user(
                task,
                assignee_user_id=record.from_user_id,
                assignee_queue_id=None,
            )
            await task_repo.update(new_task)
            restored.append(new_task)
            await audit.append(
                tenant_id=cmd.tenant_id,
                event_type=TASK_DELEGATION_EXPIRED,
                actor_id=record.from_user_id,
                actor_type="system",
                subject_type=SUBJECT_TYPE_TASK,
                subject_id=new_task.id,
                case_id=new_task.case_id,
                correlation_id=cmd.correlation_id,
                causation_id=None,
                payload={
                    "restored_user_id": str(record.from_user_id),
                    "delegate_user_id": str(record.to_user_id),
                    "until": record.until.isoformat(),
                },
                occurred_at=now,
            )
            await outbox.enqueue(
                tenant_id=cmd.tenant_id,
                event_type=TASK_DELEGATION_EXPIRED,
                event_id=uuid4(),
                payload={
                    "task_id": str(new_task.id),
                    "case_id": str(new_task.case_id),
                    "restored_user_id": str(record.from_user_id),
                    "delegate_user_id": str(record.to_user_id),
                    "tenant_id": str(cmd.tenant_id),
                    "actor_id": str(record.from_user_id),
                    "occurred_at": now.isoformat(),
                },
            )
        else:
            superseded.append(record.task_id)
        await delegation_store.remove(cmd.tenant_id, record.task_id)
    return ExpireDelegationsResult(restored=tuple(restored), superseded=tuple(superseded))
