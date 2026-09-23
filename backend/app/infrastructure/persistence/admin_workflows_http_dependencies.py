"""SQL composition provider for the admin workflow HTTP boundary.

This adapter stays outside ``app.interfaces``. It binds the local SQLAlchemy
repositories and audit store to the infra-free admin workflow provider
contract; an Azure adapter can implement the same contract later.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workflow.http_dependencies import (
    AdminWorkflowsHttpRepositories,
)
from app.domain.workflow import WorkflowDefinition, WorkflowVersion
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore
from app.infrastructure.persistence.repositories import (
    SqlQueueRepository,
    SqlRoleRepository,
    SqlWorkflowDefinitionRepository,
    SqlWorkflowVersionRepository,
)


class SqlAdminWorkflowsHttpDependencyProvider:
    """Bind all admin workflow HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> AdminWorkflowsHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return AdminWorkflowsHttpRepositories(
            workflow=SqlWorkflowDefinitionRepository(db),
            workflow_version=SqlWorkflowVersionRepository(db),
            queue=SqlQueueRepository(db),
            audit=SqlAuditEventStore(db),
            role=SqlRoleRepository(db),
            workflow_read=_SqlWorkflowReadRepository(db),
        )


class _SqlWorkflowReadRepository:
    """Compose the read-only workflow projections from SQL repositories."""

    def __init__(self, session: AsyncSession) -> None:
        self._definitions = SqlWorkflowDefinitionRepository(session)
        self._versions = SqlWorkflowVersionRepository(session)

    async def list_workflows(self, tenant_id: UUID) -> list[WorkflowDefinition]:
        """Return tenant-scoped workflow headers."""
        return await self._definitions.list(tenant_id)

    async def list_versions(
        self, tenant_id: UUID, workflow_definition_id: UUID
    ) -> list[WorkflowVersion]:
        """Return tenant-scoped workflow versions."""
        return await self._versions.list_versions(tenant_id, workflow_definition_id)


__all__ = ["SqlAdminWorkflowsHttpDependencyProvider"]
