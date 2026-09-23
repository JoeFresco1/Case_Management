"""Queue administration use cases: define, reconfigure, retire (P02-US3, US9 / FM-016).

Admin operations require ``ADMIN_TENANT_CONFIG`` (plus the local
``process_admin`` guard so a non-admin caller never reaches the database).
Every repository call is tenant-scoped; cross-tenant ids surface as
:class:`NotFound` so the API never confirms cross-tenant existence
(Constitution V). Each operation writes the queue change, an audit row,
and an outbox event in the same DB transaction.

Split out of the former single-file ``manage_queues.py`` during the R2
package split; the worker-facing operations live in :mod:`.claims` and
:mod:`.delegations`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.audit import QUEUE_DEFINED, QUEUE_RECONFIGURED, QUEUE_RETIRED
from app.domain.context import UserContext
from app.domain.errors import Conflict, InvariantViolation, NotFound
from app.domain.queue import Queue

from .commands import DefineQueueCommand, ReconfigureQueueCommand, RetireQueueCommand
from .ports import (
    SUBJECT_TYPE_QUEUE,
    _AuditStore,
    _Clock,
    _OutboxWriter,
    _QueueRepository,
)


def _require_admin(actor: UserContext, tenant_id: UUID) -> None:
    """Raise ``Forbidden`` if the actor lacks ``ADMIN_TENANT_CONFIG``.

    Mirrors the local helper in :mod:`app.application.roles.manage_roles`
    so a non-admin caller never reaches the database.
    """
    if "process_admin" not in actor.roles:
        raise InvariantViolation("queue administration requires process_admin role")
    _ = tenant_id


async def define_queue(
    cmd: DefineQueueCommand,
    *,
    queue_repo: _QueueRepository,
    audit: _AuditStore,
    outbox: _OutboxWriter,
    clock: _Clock,
) -> Queue:
    """Create a new queue (P02-US3, US9 / FM-016)."""
    _require_admin(cmd.actor, cmd.tenant_id)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="queue"),
    )
    existing = await queue_repo.get_by_key(cmd.tenant_id, cmd.key)
    if existing is not None:
        raise Conflict(f"queue with key {cmd.key!r} already exists")
    now = clock.now()
    queue = Queue.create(
        tenant_id=cmd.tenant_id,
        key=cmd.key,
        name=cmd.name,
        description=cmd.description,
        assignment_policy=cmd.assignment_policy,
        capacity=cmd.capacity,
        member_user_ids=cmd.member_user_ids,
        visibility_scope=cmd.visibility_scope,
        created_at=now,
        updated_at=now,
    )
    await queue_repo.add(queue)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=QUEUE_DEFINED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_QUEUE,
        subject_id=queue.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "queue_key": queue.key,
            "queue_name": queue.name,
            "assignment_policy": queue.assignment_policy.value,
            "capacity": queue.capacity,
            "member_user_ids": [str(uid) for uid in queue.member_user_ids],
            "visibility_scope": queue.visibility_scope.value,
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=QUEUE_DEFINED,
        event_id=uuid4(),
        payload={
            "queue_id": str(queue.id),
            "queue_key": queue.key,
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor.user_id),
            "occurred_at": now.isoformat(),
        },
    )
    return queue


async def reconfigure_queue(
    cmd: ReconfigureQueueCommand,
    *,
    queue_repo: _QueueRepository,
    audit: _AuditStore,
    outbox: _OutboxWriter,
    clock: _Clock,
) -> Queue:
    """Apply a configuration change to an existing queue.

    Only the fields the command carries are touched; passing
    ``None`` for a field is a no-op. ``member_user_ids`` with an
    empty tuple clears the list (legal under ``MANUAL``; rejected
    under ``ROUND_ROBIN`` by the aggregate).
    """
    _require_admin(cmd.actor, cmd.tenant_id)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="queue"),
    )
    queue = await queue_repo.get(cmd.tenant_id, cmd.queue_id)
    if queue is None:
        raise NotFound(f"queue {cmd.queue_id} not found")
    next_queue = queue
    if cmd.name is not None:
        next_queue = next_queue.rename(name=cmd.name)
    if cmd.description is not None:
        next_queue = next_queue.set_description(description=cmd.description)
    if cmd.assignment_policy is not None or cmd.capacity is not None:
        next_queue = next_queue.configure_policy(
            assignment_policy=cmd.assignment_policy or next_queue.assignment_policy,
            capacity=cmd.capacity,
        )
    if cmd.member_user_ids is not None:
        next_queue = next_queue.set_members(member_user_ids=cmd.member_user_ids)
    if cmd.visibility_scope is not None:
        next_queue = next_queue.set_visibility_scope(visibility_scope=cmd.visibility_scope)
    now = clock.now()
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
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=QUEUE_RECONFIGURED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_QUEUE,
        subject_id=next_queue.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "queue_key": next_queue.key,
            "assignment_policy": next_queue.assignment_policy.value,
            "capacity": next_queue.capacity,
            "member_count": len(next_queue.member_user_ids),
            "visibility_scope": next_queue.visibility_scope.value,
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=QUEUE_RECONFIGURED,
        event_id=uuid4(),
        payload={
            "queue_id": str(next_queue.id),
            "queue_key": next_queue.key,
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor.user_id),
            "occurred_at": now.isoformat(),
        },
    )
    return next_queue


async def retire_queue(
    cmd: RetireQueueCommand,
    *,
    queue_repo: _QueueRepository,
    audit: _AuditStore,
    outbox: _OutboxWriter,
    clock: _Clock,
) -> UUID:
    """Retire a queue (P10 lifecycle)."""
    _require_admin(cmd.actor, cmd.tenant_id)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        cmd.actor,
        Resource(tenant_id=cmd.tenant_id, kind="queue"),
    )
    queue = await queue_repo.get(cmd.tenant_id, cmd.queue_id)
    if queue is None:
        raise NotFound(f"queue {cmd.queue_id} not found")
    now = clock.now()
    await queue_repo.delete(cmd.tenant_id, queue.id)
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=QUEUE_RETIRED,
        actor_id=cmd.actor.user_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_QUEUE,
        subject_id=queue.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "queue_key": queue.key,
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=QUEUE_RETIRED,
        event_id=uuid4(),
        payload={
            "queue_id": str(queue.id),
            "queue_key": queue.key,
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor.user_id),
            "occurred_at": now.isoformat(),
        },
    )
    return queue.id
