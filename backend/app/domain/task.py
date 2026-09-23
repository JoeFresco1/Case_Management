"""Task aggregate (P02-US1 / FM-014 / FR-011).

A ``Task`` is a unit of work the workflow engine assigns to a user,
queue, or system actor. Tasks are created by workflow transition
``actions`` of type ``create_task`` (see
``app.domain.workflow.WorkflowTransition.actions``) or by
``start_workflow`` for the workflow's initial tasks (P02-US1 / T070).

# Lifecycle

``Task.status`` is one of:

- ``open`` — created, not yet worked. Mutable (reassign, due-date
  change, cancel).
- ``completed`` — closed with an ``outcome``. Immutable (the aggregate
  rejects any further ``complete`` call and any other status-flip).
- ``cancelled`` — closed without completion (e.g. case withdrawn,
  workflow retired mid-flight). Immutable.

# Idempotent completion (FR-011 / P02-US1 acceptance scenario 1)

A duplicate completion against the **same task** is a no-op: the
aggregate returns the existing instance with its prior ``completed_at``
and ``outcome`` unchanged, and does not bump ``version_token``. This is
the **domain-level** idempotency. The HTTP layer additionally replays
responses by ``Idempotency-Key`` so a network-retry after a 200
produces the same wire shape, never a duplicate ``TaskCompleted`` audit
row.

The two layers are deliberately independent:

- Domain idempotency protects a within-process duplicate call
  (``complete`` twice in the same handler, two concurrent dispatches).
- HTTP idempotency protects a network-retry that lost its 200.

# version_token (Constitution V — no silent overwrites)

Each status flip bumps ``version_token``. The repository's ``update``
compares the row's stored token against the aggregate's token and
raises :class:`Conflict` when they differ — same shape as the case
aggregate (P01-US3).

# INTEGRATION_SEAM: TaskRepository

``app.domain.ports.TaskRepository`` is the persistence Protocol; the
SQL adapter lands in T063 / T071 alongside the workflow adapters. The
domain layer depends on the Protocol only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation


class TaskStatus(StrEnum):
    """Lifecycle states for a Task."""

    OPEN = "open"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Task:
    """A unit of work on a case (P02-US1 / FM-014 / FR-011).

    The aggregate is immutable: every status flip (``complete``,
    ``cancel``) returns a new instance with an incremented
    ``version_token``. The repository persists the new shape and
    enforces the token check on the way down.

    ``outcome`` is the structured closure detail (``{"decision":
    "approved", "notes": "..."}``) captured at completion time. It is
    a free-form ``dict`` because v1 has no closed enumeration; the
    workflow author reads the keys they wrote. ``outcome`` is
    tenant-internal — never embedded in outbox payloads, only
    referenced by ``task_id``.
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    type: str
    title: str
    status: TaskStatus = TaskStatus.OPEN
    priority: str = "normal"
    assignee_user_id: UUID | None = None
    assignee_queue_id: str | None = None
    due_at: datetime | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    completed_by: UUID | None = None
    outcome: dict[str, object] | None = None
    # Optimistic concurrency token. Bumped on every status flip so a
    # concurrent completion is refused with Conflict (Constitution V).
    version_token: int = 1
    # The transition key that produced this task, or the literal
    # ``"initial"`` when created by ``start_workflow``. Tenant-internal;
    # the field makes audit replay ("which transition fired this?")
    # trivial without re-reading the workflow version.
    created_by_transition: str | None = None

    # ----- factories --------------------------------------------------------

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        case_id: UUID,
        type: str,
        title: str,
        priority: str = "normal",
        assignee_user_id: UUID | None = None,
        assignee_queue_id: str | None = None,
        due_at: datetime | None = None,
        created_at: datetime | None = None,
        created_by_transition: str | None = None,
    ) -> Task:
        """Construct a new ``open`` task.

        The constructor enforces the invariants the workflow engine
        relies on: tenant matches case, both ids are non-null, and the
        ``type`` and ``title`` are non-empty. A violation raises
        :class:`InvariantViolation`.
        """
        if tenant_id is None or case_id is None:
            raise InvariantViolation("tenant_id and case_id are required")
        if not type or not type.strip():
            raise InvariantViolation("task type is required")
        if not title or not title.strip():
            raise InvariantViolation("task title is required")
        if assignee_user_id is None and not assignee_queue_id:
            # Why fail-closed: a task with no one to work it has no
            # owner and no destination. The workflow engine surfaces
            # the error before the row is staged.
            raise InvariantViolation(
                "task requires either an assignee_user_id or assignee_queue_id"
            )
        return Task(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            type=type.strip(),
            title=title.strip(),
            status=TaskStatus.OPEN,
            priority=priority,
            assignee_user_id=assignee_user_id,
            assignee_queue_id=assignee_queue_id,
            due_at=due_at,
            created_at=created_at,
            completed_at=None,
            completed_by=None,
            outcome=None,
            version_token=1,
            created_by_transition=created_by_transition,
        )

    # ----- transitions ------------------------------------------------------

    def complete(
        self,
        *,
        actor_id: UUID,
        outcome: dict[str, object] | None,
        at: datetime,
    ) -> Task:
        """Mark the task completed with the actor's outcome (FR-011).

        Idempotent: a second call against an already-``completed`` task
        returns the existing instance unchanged (same ``completed_at``,
        same ``outcome``, same ``version_token``). This is the
        **domain-level** idempotency that protects within-process
        duplicates; the HTTP layer adds ``Idempotency-Key`` replay on
        top.

        A call against a ``cancelled`` task raises
        :class:`InvariantViolation` — a cancelled task cannot be
        resurrected.
        """
        if self.status is TaskStatus.COMPLETED:
            return self
        if self.status is TaskStatus.CANCELLED:
            raise InvariantViolation(f"task {self.id} is cancelled and cannot be completed")
        return replace(
            self,
            status=TaskStatus.COMPLETED,
            completed_at=at,
            completed_by=actor_id,
            outcome=outcome,
            version_token=self.version_token + 1,
        )

    def cancel(self, *, at: datetime, reason: str | None = None) -> Task:
        """Cancel an open task (irreversible).

        Cancellation is the engine's way to retire a follow-on task
        after a transition's guard fails on a sibling branch, or to
        withdraw a task when a case is closed mid-flight. A
        ``completed`` or already-``cancelled`` task is left alone — the
        second call is a no-op, same idempotency shape as
        :meth:`complete`.
        """
        if self.status is not TaskStatus.OPEN:
            return self
        _ = reason  # The reason is an audit-payload detail, not a Task field.
        return replace(
            self,
            status=TaskStatus.CANCELLED,
            completed_at=at,
            version_token=self.version_token + 1,
        )


__all__ = [
    "Task",
    "TaskStatus",
]
