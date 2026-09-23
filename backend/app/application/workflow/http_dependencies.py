"""Dependency contracts for the admin workflow HTTP composition boundary.

The workflow router needs several ports bound to one request transaction.
This module describes that bundle without importing FastAPI, SQLAlchemy, or
any infrastructure adapter. The composition root supplies the concrete
provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.domain.ports import (
    AuditEventStore,
    QueueRepository,
    RoleRepository,
    WorkflowDefinitionRepository,
    WorkflowVersionRepository,
)
from app.domain.workflow import WorkflowDefinition, WorkflowVersion


class WorkflowReadRepository(Protocol):
    """Read-only workflow projection port for the admin UI."""

    async def list_workflows(self, tenant_id: UUID) -> list[WorkflowDefinition]:
        """Return workflow headers visible to ``tenant_id``."""

    async def list_versions(
        self, tenant_id: UUID, workflow_definition_id: UUID
    ) -> list[WorkflowVersion]:
        """Return versions for one tenant-scoped workflow header."""


@dataclass(frozen=True, slots=True)
class AdminWorkflowsHttpRepositories:
    """Ports required by the admin workflow endpoints for one session."""

    workflow: WorkflowDefinitionRepository
    workflow_version: WorkflowVersionRepository
    queue: QueueRepository
    audit: AuditEventStore
    role: RoleRepository
    workflow_read: WorkflowReadRepository | None = None


class AdminWorkflowsHttpDependencyProvider(Protocol):
    """Create a request-scoped admin workflow port bundle from a session."""

    def for_session(self, session: object) -> AdminWorkflowsHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "AdminWorkflowsHttpDependencyProvider",
    "AdminWorkflowsHttpRepositories",
    "WorkflowReadRepository",
]
