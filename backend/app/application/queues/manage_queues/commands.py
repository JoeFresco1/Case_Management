"""Command / result dataclasses for the queue use cases (P02-US3, US9 / FM-016).

Frozen value objects passed from the HTTP layer into the use-case functions
in :mod:`app.application.queues.manage_queues.admin`,
:mod:`.claims`, and :mod:`.delegations`. Split out of the former
single-file ``manage_queues.py`` during the R2 package split; no behavior
lives here — only typed inputs and outputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.domain.context import UserContext
from app.domain.queue import AssignmentPolicy, Queue, VisibilityScope


@dataclass(frozen=True, slots=True)
class DefineQueueCommand:
    """Input to :func:`~app.application.queues.manage_queues.define_queue`."""

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    key: str
    name: str
    description: str = ""
    assignment_policy: AssignmentPolicy = AssignmentPolicy.MANUAL
    capacity: int = 0
    member_user_ids: tuple[UUID, ...] = ()
    visibility_scope: VisibilityScope = VisibilityScope.TENANT


@dataclass(frozen=True, slots=True)
class ReconfigureQueueCommand:
    """Input to :func:`~app.application.queues.manage_queues.reconfigure_queue`.

    Every field except ``queue_id``, ``tenant_id``, ``actor`` and
    ``correlation_id`` is optional. ``None`` means "leave
    unchanged"; an empty tuple for ``member_user_ids`` means
    "clear the membership list" (only legal under ``MANUAL``).
    """

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    queue_id: UUID
    name: str | None = None
    description: str | None = None
    assignment_policy: AssignmentPolicy | None = None
    capacity: int | None = None
    member_user_ids: tuple[UUID, ...] | None = None
    visibility_scope: VisibilityScope | None = None


@dataclass(frozen=True, slots=True)
class RetireQueueCommand:
    """Input to :func:`~app.application.queues.manage_queues.retire_queue`."""

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    queue_id: UUID


@dataclass(frozen=True, slots=True)
class ClaimNextCommand:
    """Input to :func:`~app.application.queues.manage_queues.claim_next`.

    ``queue_key`` is the stable identifier; ``actor_id`` is the
    claiming worker (must be a tenant member — not enforced here,
    the policy treats any user with ``TASK_READ`` as eligible).
    """

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    queue_key: str


@dataclass(frozen=True, slots=True)
class ClaimNextResult:
    """Post-claim shape returned to the router."""

    task: Any
    queue: Queue


@dataclass(frozen=True, slots=True)
class ReleaseTaskCommand:
    """Input to :func:`~app.application.queues.manage_queues.release_task`."""

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    task_id: UUID
    reason: str | None = None
    # Target queue when the task no longer carries one. A task claimed
    # through claim_next has ``assignee_queue_id = None`` (the claim
    # moves it off the queue), so releasing it back needs the caller
    # to name the queue. When the task still carries a prior queue
    # that value wins; this is only a fallback.
    queue_key: str | None = None


@dataclass(frozen=True, slots=True)
class BulkHandleCommand:
    """Bulk claim or release (P02-US3 / FM-016).

    ``action`` is ``"claim"`` or ``"release"``; ``task_ids`` is the
    batch. The whole operation is atomic: any per-task failure
    rolls back the prior updates so a partial bulk never lands.
    """

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    action: str
    task_ids: tuple[UUID, ...]
    queue_key: str | None = None


@dataclass(frozen=True, slots=True)
class BulkHandleResult:
    """Handled tasks plus the ids skipped (missing / cross-tenant)."""

    handled: tuple[Any, ...]
    skipped: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class DelegateCommand:
    """Input to :func:`~app.application.queues.manage_queues.delegate` (P02-US9).

    ``delegate_user_id`` is the new assignee; ``until`` is the
    exclusive bound on the delegation window (the delegate loses
    the task when ``now > until``; expiry restores the prior owner
    via :func:`~app.application.queues.manage_queues.expire_delegations`
    once a ``delegation_store`` is wired — without one the window
    lives on the audit row only).
    """

    tenant_id: UUID
    actor: UserContext
    correlation_id: str
    delegate_user_id: UUID
    task_ids: tuple[UUID, ...]
    until: datetime


@dataclass(frozen=True, slots=True)
class ExpireDelegationsCommand:
    """Input to :func:`~app.application.queues.manage_queues.expire_delegations`.

    System-initiated (no actor auth — the sweep runs as the platform,
    tenant-scoped by ``tenant_id`` like the SLA scanner). ``now``
    comes from the injected clock; tests advance it past ``until``.
    """

    tenant_id: UUID
    correlation_id: str


@dataclass(frozen=True, slots=True)
class ExpireDelegationsResult:
    """Outcome of :func:`~app.application.queues.manage_queues.expire_delegations`.

    ``restored`` are the tasks handed back to their prior owners;
    ``superseded`` are delegation records dropped without a restore
    (task completed, cancelled, or reassigned away from the delegate
    before expiry — there is nothing to hand back).
    """

    restored: tuple[Any, ...]
    superseded: tuple[UUID, ...]
