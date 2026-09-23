"""Task, Queue, WorkflowInstance, and WorkflowTransition ports (P02 / FM-014, FM-016)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.ports.core import TenantScoped
from app.domain.workflow import WorkflowTransitionRecord


class QueueRepository(TenantScoped, Protocol):
    """Queue lookup + configuration port (P02-US3, US9 / FM-016).

    The application layer uses this port for the queue admin CRUD
    surface (``define`` / ``update`` / ``retire`` / ``list`` /
    ``get``) and the worker-facing claim / release / delegate flow.
    Tenant isolation is mandatory (AGENTS.md §3 R1, §12): every
    method takes ``tenant_id`` first and every implementation
    includes ``WHERE tenant_id = :tenant_id``.

    The first-slice SQL adapter is a stub pending T066-T077. The
    application use cases accept the Protocol and the test suite
    supplies an in-memory implementation so the orchestration can
    proceed without the SQL adapter being on the critical path.
    """

    async def list_keys(self, tenant_id: UUID) -> list[str]:
        """Return the set of queue keys configured for this tenant.

        Order is not significant -- callers use the set for membership
        checks.
        """

    async def list_queues(self, tenant_id: UUID) -> list[Any]:
        """Return every queue configured for this tenant.

        Order is by ``key`` ascending so the admin UI renders a
        stable list.
        """

    async def get(self, tenant_id: UUID, queue_id: UUID) -> Any | None:
        """Fetch one queue by id, scoped to tenant.

        Returns ``None`` for missing or cross-tenant ids -- the two
        cases are intentionally indistinguishable so the API never
        confirms cross-tenant existence (Constitution V).
        """

    async def get_by_key(self, tenant_id: UUID, key: str) -> Any | None:
        """Fetch one queue by stable key, scoped to tenant."""

    async def add(self, queue: Any) -> None:
        """Persist a new queue row.

        Raises :class:`Conflict` if the (tenant_id, key) tuple is
        already taken -- the use case treats that as a 409.
        """

    async def update(self, queue: Any) -> None:
        """Persist a configuration change to an existing queue.

        Implementations MUST enforce optimistic concurrency on the
        queue's ``version_token`` so a concurrent ``update`` is
        refused with :class:`Conflict` (Constitution V).
        """

    async def delete(self, tenant_id: UUID, queue_id: UUID) -> None:
        """Retire a queue (P10 lifecycle -- soft, not cascade).

        Hard DELETE is reserved for tests; production retires by
        marking the row ``status='retired'`` (out of scope for v1).
        This port method exists for symmetry with the role admin
        surface so future slices land the same shape.
        """


class TaskRepository(TenantScoped, Protocol):
    """Task aggregate persistence (P02-US1 / FM-014 / FR-011).

    The first-slice surface is intentionally minimal: the use case needs
    ``add`` (create the row), ``get`` (resolve by id, tenant-scoped),
    and ``update`` (persist a status flip -- the aggregate bumps an
    internal ``version_token`` so a concurrent editor's write is not
    silently overwritten).

    Idempotency on completion is enforced at the **domain** layer
    (:meth:`Task.complete`) -- a second call against an already
    ``completed`` task returns the prior aggregate unchanged. The
    HTTP layer additionally replays responses by ``Idempotency-Key`` so
    a network-retry after a 200 produces the same wire shape, never a
    duplicate audit row (FR-011 / AGENTS.md §10).

    Every method is tenant-scoped: implementations include
    ``WHERE tenant_id = :tenant_id`` in every query, the same way the
    case / workflow repositories do.
    """

    async def add(self, task: Any) -> None:
        """Persist a new task. The ``tenant_id`` comes from the aggregate."""

    async def update(self, task: Any) -> None:
        """Persist changes to an existing task.

        Implementations MUST enforce optimistic concurrency on the
        task's internal ``version_token`` so a concurrent completion
        against an already-completed row is refused with
        :class:`Conflict`.
        """

    async def get(self, tenant_id: UUID, task_id: UUID) -> Any | None:
        """Fetch one task by id, scoped to tenant.

        Returns ``None`` for missing or cross-tenant ids -- the two
        cases are intentionally indistinguishable so the API never
        confirms cross-tenant existence (Constitution V).
        """

    async def list_for_case(self, tenant_id: UUID, case_id: UUID) -> list[Any]:
        """Return every task attached to ``case_id``, oldest first.

        Tenant-scoped; a cross-tenant ``case_id`` returns the empty
        list. Used by the transition engine to detect already-
        materialized welcome tasks before firing ``create_task``
        actions (double-fire guard).
        """

    async def list_open_for_queue(
        self,
        tenant_id: UUID,
        queue_key: str,
        *,
        limit: int = 100,
    ) -> list[Any]:
        """Return open tasks currently assigned to ``queue_key``.

        Order: ``due_at ASC NULLS LAST, created_at ASC`` so the
        most-urgent work surfaces first. Tenant-scoped; a
        cross-tenant queue key surfaces an empty list (the queue
        is invisible from this tenant -- never confirmed).
        """

    async def list_open_for_user(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        limit: int = 100,
    ) -> list[Any]:
        """Return open tasks currently assigned to ``user_id``.

        Same ordering as ``list_open_for_queue``.
        """

    async def list_for_actor(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        queue_key: str | None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Any]:
        """List open and terminal tasks for one user or queue."""

    async def list_overdue_for_queues(
        self,
        tenant_id: UUID,
        *,
        queue_keys: tuple[str, ...],
        now: datetime,
        limit: int = 200,
    ) -> list[Any]:
        """Return overdue open tasks assigned to any queue in ``queue_keys``.

        P12-US2 / FM-051: a task is overdue iff ``status = 'open'``
        AND ``due_at <= now``. Tasks without a ``due_at`` are never
        overdue (no SLA window = no deadline). Implementations MUST
        include ``WHERE tenant_id = :tenant_id`` and ``assignee_queue_id
        IN :queue_keys``. Order: ``due_at ASC`` so the most overdue
        work surfaces first. Cross-tenant queue keys are silently
        dropped (the queue is invisible from this tenant).
        """


class WorkflowInstanceRepository(TenantScoped, Protocol):
    """WorkflowInstance aggregate persistence (P02-US1 / FM-014 / FR-009).

    The instance pins the published ``workflow_version_id`` it was
    started against and records the current state. New transitions
    update the pointer via ``update``; the use case stages the audit and
    outbox rows in the same transaction so the instance pointer, the
    audit row, and the outbox event commit together (Constitution III).

    Tenant isolation is mandatory: every method takes ``tenant_id``
    first, every implementation includes ``WHERE tenant_id =
    :tenant_id`` (AGENTS.md §10 / §12).
    """

    async def add(self, instance: Any) -> None:
        """Persist a new WorkflowInstance row."""

    async def get(self, tenant_id: UUID, instance_id: UUID) -> Any | None:
        """Fetch one WorkflowInstance by id, scoped to tenant."""

    async def get_for_case(self, tenant_id: UUID, case_id: UUID) -> Any | None:
        """Return the (single) WorkflowInstance for a case, if any.

        A case owns at most one workflow instance in v1; the unique
        index on ``(tenant_id, case_id)`` makes the lookup safe under
        concurrency. Returns ``None`` for missing or cross-tenant case
        ids.
        """

    async def update(self, instance: Any) -> None:
        """Persist a state-pointer change.

        Implementations MUST refuse to overwrite when the stored
        ``version_token`` differs from the aggregate's token (Constitution
        V -- no silent overwrites).
        """

    async def bump_state(
        self,
        *,
        tenant_id: UUID,
        instance_id: UUID,
        new_state: str,
        expected_version_token: int,
        occurred_at: datetime,
    ) -> Any:
        """Move the instance pointer to ``new_state`` atomically.

        Implementations MUST refuse to overwrite when the stored
        ``version_token`` differs from ``expected_version_token`` so a
        concurrent transition cannot silently win (Constitution V --
        no silent overwrites). The returned shape is the post-update
        instance the caller can read for ``current_state`` /
        ``updated_at`` / ``version_token``.

        Why a dedicated method (not ``update``): the state-pointer
        change is the only mutation a transition engine performs; a
        narrowly-typed primitive lets the SQL adapter enforce the
        token check at the persistence boundary without exposing the
        full instance shape through the port.
        """


class WorkflowTransitionLog(TenantScoped, Protocol):
    """Append-only log of executed workflow transitions (T069).

    Each row records that a declared edge fired for an instance. The
    engine stages the row in the same transaction as the instance
    bump, the case update, the audit row, and the outbox event. No
    UPDATE/DELETE surface exists at any layer.
    """

    async def record(self, entry: WorkflowTransitionRecord) -> None:
        """Persist one executed-transition record."""


__all__ = [
    "QueueRepository",
    "TaskRepository",
    "WorkflowInstanceRepository",
    "WorkflowTransitionLog",
]
