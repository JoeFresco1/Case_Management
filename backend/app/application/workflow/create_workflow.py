"""Create a tenant-scoped workflow definition header (FM-013 / T075)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.errors import Conflict
from app.domain.workflow import WorkflowDefinition


class _WorkflowRepository(Protocol):
    """Minimal workflow port needed by the create operation."""

    async def get_by_key(self, tenant_id: UUID, key: str) -> WorkflowDefinition | None: ...

    async def add(self, workflow: WorkflowDefinition) -> None: ...


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _AuditStore(Protocol):
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


@dataclass(frozen=True, slots=True)
class CreateWorkflowCommand:
    """Input for creating a workflow definition header."""

    tenant_id: UUID
    actor_id: UUID
    key: str
    name: str
    description: str
    correlation_id: str


async def handle(
    cmd: CreateWorkflowCommand,
    *,
    workflow_repo: _WorkflowRepository,
    audit_store: _AuditStore,
    clock: _Clock,
) -> WorkflowDefinition:
    """Create a workflow header and append its configuration audit event."""
    if await workflow_repo.get_by_key(cmd.tenant_id, cmd.key) is not None:
        raise Conflict(f"workflow definition {cmd.key!r} already exists")
    now = clock.now()
    workflow = WorkflowDefinition.create(
        tenant_id=cmd.tenant_id,
        key=cmd.key,
        name=cmd.name,
        description=cmd.description,
        now=now,
    )
    await workflow_repo.add(workflow)
    await audit_store.append(
        tenant_id=cmd.tenant_id,
        event_type="ConfigurationDrafted",
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="WorkflowDefinition",
        subject_id=workflow.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={"workflow_key": workflow.key, "name": workflow.name},
        occurred_at=now,
    )
    return workflow


__all__ = ["CreateWorkflowCommand", "handle"]
