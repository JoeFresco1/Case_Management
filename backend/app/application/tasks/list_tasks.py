"""List tenant-visible tasks for the worker workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.context import UserContext
from app.domain.task import Task


@dataclass(frozen=True, slots=True)
class ListTasksCommand:
    """Validated worker task-list filters."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    assignee: str | None
    status: str
    limit: int
    offset: int


class _TaskLister(Protocol):
    async def list_open_for_queue(
        self, tenant_id: UUID, queue_key: str, *, limit: int
    ) -> list[Task]: ...

    async def list_open_for_user(
        self, tenant_id: UUID, user_id: UUID, *, limit: int
    ) -> list[Task]: ...

    async def list_for_actor(
        self,
        tenant_id: UUID,
        user_id: UUID,
        *,
        queue_key: str | None,
        limit: int,
        offset: int,
    ) -> list[Task]: ...


async def handle(cmd: ListTasksCommand, *, task_repo: _TaskLister) -> list[Task]:
    """Authorize and return tasks scoped to the actor and tenant."""
    await authorize(
        Action.TASK_READ,
        UserContext(
            user_id=cmd.actor_id, tenant_id=cmd.tenant_id, display_name="", roles=cmd.actor_roles
        ),
        Resource(tenant_id=cmd.tenant_id, kind="task"),
    )
    queue_key = None
    if cmd.assignee and cmd.assignee.startswith("queue:"):
        queue_key = cmd.assignee[6:]
    if cmd.status == "open" and queue_key:
        return await task_repo.list_open_for_queue(cmd.tenant_id, queue_key, limit=cmd.limit)
    if cmd.status == "open" and (cmd.assignee in (None, "me")):
        return await task_repo.list_open_for_user(cmd.tenant_id, cmd.actor_id, limit=cmd.limit)
    return await task_repo.list_for_actor(
        cmd.tenant_id, cmd.actor_id, queue_key=queue_key, limit=cmd.limit, offset=cmd.offset
    )
