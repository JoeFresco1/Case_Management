"""Portable SQL adapter for the tenant-scoped task repository."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import update as sql_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import Conflict
from app.domain.task import Task
from app.infrastructure.persistence.mapping import task_from_domain, task_to_domain
from app.infrastructure.persistence.models import Task as TaskRow


class SqlTaskRepository:
    """Persist tasks with explicit tenant filters and compare-and-swap updates."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, task: Task) -> None:
        """Stage a new task in the caller's transaction."""
        self._s.add(TaskRow(**task_from_domain(task)))

    async def update(self, task: Task) -> None:
        """Update a task only when its previously observed token still matches."""
        values = task_from_domain(task)
        for key in ("id", "tenant_id", "created_at"):
            values.pop(key, None)
        stmt = (
            sql_update(TaskRow)
            .where(
                TaskRow.tenant_id == task.tenant_id,
                TaskRow.id == task.id,
                TaskRow.version_token == task.version_token - 1,
            )
            .values(**values)
        )
        result = await self._s.execute(stmt)
        if not isinstance(result, CursorResult) or result.rowcount == 0:
            raise Conflict(f"stale concurrency token for task {task.id}")

    async def get(self, tenant_id: UUID, task_id: UUID) -> Task | None:
        """Fetch a task, returning no result for missing or cross-tenant ids."""
        stmt = select(TaskRow).where(TaskRow.tenant_id == tenant_id, TaskRow.id == task_id)
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return task_to_domain(row) if row else None

    async def list_for_case(self, tenant_id: UUID, case_id: UUID) -> list[Task]:
        """List every task on a case, oldest first, scoped to tenant."""
        stmt = (
            select(TaskRow)
            .where(TaskRow.tenant_id == tenant_id, TaskRow.case_id == case_id)
            .order_by(TaskRow.created_at.asc())
        )
        return [task_to_domain(row) for row in (await self._s.execute(stmt)).scalars().all()]

    async def list_open_for_queue(
        self, tenant_id: UUID, queue_key: str, *, limit: int = 100
    ) -> list[Task]:
        """List open tasks assigned to a tenant's queue in urgency order."""
        stmt = (
            select(TaskRow)
            .where(
                TaskRow.tenant_id == tenant_id,
                TaskRow.assignee_queue_id == queue_key,
                TaskRow.status == "open",
            )
            .order_by(TaskRow.due_at.asc(), TaskRow.created_at.asc())
            .limit(limit)
        )
        return [task_to_domain(row) for row in (await self._s.execute(stmt)).scalars().all()]

    async def list_open_for_user(
        self, tenant_id: UUID, user_id: UUID, *, limit: int = 100
    ) -> list[Task]:
        """List open tasks assigned to a tenant's user in urgency order."""
        stmt = (
            select(TaskRow)
            .where(
                TaskRow.tenant_id == tenant_id,
                TaskRow.assignee_user_id == user_id,
                TaskRow.status == "open",
            )
            .order_by(TaskRow.due_at.asc(), TaskRow.created_at.asc())
            .limit(limit)
        )
        return [task_to_domain(row) for row in (await self._s.execute(stmt)).scalars().all()]

    async def list_for_actor(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        queue_key: str | None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Task]:
        """List all statuses for a user or queue, always scoped to the tenant."""
        filters = [TaskRow.tenant_id == tenant_id]
        if queue_key is not None:
            filters.append(TaskRow.assignee_queue_id == queue_key)
        else:
            filters.append(TaskRow.assignee_user_id == user_id)
        stmt = (
            select(TaskRow)
            .where(*filters)
            .order_by(TaskRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [task_to_domain(row) for row in (await self._s.execute(stmt)).scalars().all()]

    async def list_overdue_for_queues(
        self, tenant_id: UUID, *, queue_keys: tuple[str, ...], now: datetime, limit: int = 200
    ) -> list[Task]:
        """List overdue open tasks for explicitly supplied tenant queue keys."""
        if not queue_keys:
            return []
        stmt = (
            select(TaskRow)
            .where(
                TaskRow.tenant_id == tenant_id,
                TaskRow.assignee_queue_id.in_(queue_keys),
                TaskRow.status == "open",
                TaskRow.due_at.is_not(None),
                TaskRow.due_at <= now,
            )
            .order_by(TaskRow.due_at.asc())
            .limit(limit)
        )
        return [task_to_domain(row) for row in (await self._s.execute(stmt)).scalars().all()]


__all__ = ["SqlTaskRepository"]
