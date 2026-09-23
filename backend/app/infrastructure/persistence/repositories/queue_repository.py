"""Portable SQL adapter for the tenant-scoped queue repository (FM-016).

Implements ``app.domain.ports.QueueRepository``: queue admin CRUD
(define / list / get / update / retire) plus the ``list_keys`` lookup
the workflow validator uses to reject ``actions[].queue`` references
to unknown queues.

Every method takes ``tenant_id`` first and every query includes
``WHERE tenant_id = :tenant_id`` (AGENTS.md R10/R12). ``update``
enforces compare-and-swap on ``version_token`` so concurrent admin
edits conflict instead of silently overwriting (Constitution V).

All column types are SQLAlchemy generic types; ``member_user_ids`` is
a JSON list of UUID strings (no arrays/JSONB) per AGENTS.md R1.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy import update as sql_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import Conflict
from app.domain.queue import Queue
from app.infrastructure.persistence.mapping import queue_from_domain, queue_to_domain
from app.infrastructure.persistence.models import Queue as QueueRow


class SqlQueueRepository:
    """Tenant-scoped queue store with optimistic-concurrency updates."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list_keys(self, tenant_id: UUID) -> list[str]:
        """Return the set of queue keys configured for this tenant."""
        rows = (
            (await self._s.execute(select(QueueRow.key).where(QueueRow.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        return [str(key) for key in rows]

    async def list_queues(self, tenant_id: UUID) -> list[Queue]:
        """Return every queue for this tenant, ``key`` ascending."""
        rows = (
            (
                await self._s.execute(
                    select(QueueRow)
                    .where(QueueRow.tenant_id == tenant_id)
                    .order_by(QueueRow.key.asc())
                )
            )
            .scalars()
            .all()
        )
        return [queue_to_domain(row) for row in rows]

    async def get(self, tenant_id: UUID, queue_id: UUID) -> Queue | None:
        """Fetch one queue by id; ``None`` for missing/cross-tenant ids."""
        row = (
            await self._s.execute(
                select(QueueRow).where(QueueRow.tenant_id == tenant_id, QueueRow.id == queue_id)
            )
        ).scalar_one_or_none()
        return queue_to_domain(row) if row else None

    async def get_by_key(self, tenant_id: UUID, key: str) -> Queue | None:
        """Fetch one queue by stable key, scoped to tenant."""
        row = (
            await self._s.execute(
                select(QueueRow).where(QueueRow.tenant_id == tenant_id, QueueRow.key == key)
            )
        ).scalar_one_or_none()
        return queue_to_domain(row) if row else None

    async def add(self, queue: Queue) -> None:
        """Persist a new queue row.

        Raises:
            Conflict: when ``(tenant_id, key)`` is already taken.
        """
        try:
            self._s.add(QueueRow(**queue_from_domain(queue)))
            await self._s.flush()
        except IntegrityError as exc:
            raise Conflict(f"queue with key {queue.key!r} already exists") from exc

    async def update(self, queue: Queue) -> None:
        """Persist a configuration change with compare-and-swap.

        Raises:
            Conflict: when the stored ``version_token`` differs from
                ``queue.version_token - 1`` (a concurrent edit won).
        """
        values = queue_from_domain(queue)
        for key in ("id", "tenant_id", "created_at", "key"):
            values.pop(key, None)
        stmt = (
            sql_update(QueueRow)
            .where(
                QueueRow.tenant_id == queue.tenant_id,
                QueueRow.id == queue.id,
                QueueRow.version_token == queue.version_token - 1,
            )
            .values(**values)
        )
        result = await self._s.execute(stmt)
        if not isinstance(result, CursorResult) or result.rowcount == 0:
            raise Conflict(f"stale concurrency token for queue {queue.id}")

    async def delete(self, tenant_id: UUID, queue_id: UUID) -> None:
        """Retire a queue row (hard delete; open tasks keep their copy)."""
        await self._s.execute(
            delete(QueueRow).where(QueueRow.tenant_id == tenant_id, QueueRow.id == queue_id)
        )


__all__ = ["SqlQueueRepository"]
