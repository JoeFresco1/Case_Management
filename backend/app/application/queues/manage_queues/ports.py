"""Local port Protocols + audit subject constants for the queue use cases.

These Protocols keep :mod:`app.application.queues.manage_queues` free of
infrastructure imports (R10): the use cases talk to the contracts only and
the composition root binds adapters (or in-memory test doubles). Split out
of the former single-file ``manage_queues.py`` during the R2 package split.

# INTEGRATION_SEAM: TaskRepository

The stub ``SqlTaskRepository`` (T063/T071) ships in a follow-up dispatch.
The use case talks to the Protocol; the in-memory test double substitutes
until the SQL adapter lands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.queue import Delegation, Queue

SUBJECT_TYPE_QUEUE = "queue"
SUBJECT_TYPE_TASK = "task"


class _Clock(Protocol):
    """Time source so tests can advance past delegation / SLA windows."""

    def now(self) -> datetime: ...


class _QueueRepository(Protocol):
    """Tenant-scoped queue persistence."""

    async def list_keys(self, tenant_id: UUID) -> list[str]: ...

    async def list_queues(self, tenant_id: UUID) -> list[Queue]: ...

    async def get(self, tenant_id: UUID, queue_id: UUID) -> Queue | None: ...

    async def get_by_key(self, tenant_id: UUID, key: str) -> Queue | None: ...

    async def add(self, queue: Queue) -> None: ...

    async def update(self, queue: Queue) -> None: ...

    async def delete(self, tenant_id: UUID, queue_id: UUID) -> None: ...


class _TaskRepository(Protocol):
    """Tenant-scoped task persistence (claim / release / bulk paths)."""

    async def get(self, tenant_id: UUID, task_id: UUID) -> Any | None: ...

    async def update(self, task: Any) -> None: ...

    async def list_open_for_queue(
        self,
        tenant_id: UUID,
        queue_key: str,
        *,
        limit: int = 100,
    ) -> list[Any]: ...

    async def list_open_for_user(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        limit: int = 100,
    ) -> list[Any]: ...


class _AuditStore(Protocol):
    """Append-only audit writer (hash-chained at the adapter)."""

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
    ) -> None: ...


class _OutboxWriter(Protocol):
    """Transactional outbox writer (events commit with the mutation)."""

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None: ...


class _CaseLookup(Protocol):
    """Read-only case resolver for policy evaluation (TASK_READ)."""

    async def get(self, tenant_id: UUID, case_id: UUID) -> Any | None: ...


class _DelegationStore(Protocol):
    """Durable store for active :class:`Delegation` records (P02-US9).

    Why a Protocol: this task ships no tables (per the dispatch
    brief), so production persistence lands with the B-5 queue-admin
    API migration. The application talks to the Protocol only; tests
    use the in-memory fake. ``remove`` is idempotent — expiring an
    already-removed record is a no-op.
    """

    async def record(self, delegation: Delegation) -> None: ...

    async def list_active(self, tenant_id: UUID) -> list[Delegation]: ...

    async def remove(self, tenant_id: UUID, task_id: UUID) -> None: ...
