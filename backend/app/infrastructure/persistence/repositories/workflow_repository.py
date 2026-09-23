"""Tenant-scoped SQL repositories for workflow configuration."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import InvariantViolation, NotFound
from app.domain.workflow import WorkflowDefinition, WorkflowVersion, WorkflowVersionStatus
from app.infrastructure.persistence.mapping import (
    workflow_definition_from_domain,
    workflow_definition_to_domain,
    workflow_version_from_domain,
    workflow_version_to_domain,
)
from app.infrastructure.persistence.models import (
    WorkflowDefinition as DefinitionRow,
)
from app.infrastructure.persistence.models import (
    WorkflowVersion as VersionRow,
)
from app.infrastructure.persistence.repositories.queue_repository import (
    SqlQueueRepository,
)

__all__ = ["SqlQueueRepository", "SqlWorkflowDefinitionRepository", "SqlWorkflowVersionRepository"]


class SqlWorkflowDefinitionRepository:
    """Persist workflow headers with explicit tenant predicates."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, tenant_id: UUID, workflow_definition_id: UUID) -> WorkflowDefinition | None:
        row = (
            await self._s.execute(
                select(DefinitionRow).where(
                    DefinitionRow.tenant_id == tenant_id, DefinitionRow.id == workflow_definition_id
                )
            )
        ).scalar_one_or_none()
        return workflow_definition_to_domain(row) if row else None

    async def get_by_key(self, tenant_id: UUID, key: str) -> WorkflowDefinition | None:
        row = (
            await self._s.execute(
                select(DefinitionRow).where(
                    DefinitionRow.tenant_id == tenant_id, DefinitionRow.key == key
                )
            )
        ).scalar_one_or_none()
        return workflow_definition_to_domain(row) if row else None

    async def add(self, value: WorkflowDefinition) -> None:
        self._s.add(DefinitionRow(**workflow_definition_from_domain(value)))

    async def list(self, tenant_id: UUID) -> list[WorkflowDefinition]:
        """List workflow headers for one tenant, in stable key order."""
        rows = (
            (
                await self._s.execute(
                    select(DefinitionRow)
                    .where(DefinitionRow.tenant_id == tenant_id)
                    .order_by(DefinitionRow.key.asc())
                )
            )
            .scalars()
            .all()
        )
        return [workflow_definition_to_domain(row) for row in rows]

    async def set_current_version(
        self, tenant_id: UUID, workflow_definition_id: UUID, version_id: UUID
    ) -> None:
        row = (
            await self._s.execute(
                select(DefinitionRow).where(
                    DefinitionRow.tenant_id == tenant_id, DefinitionRow.id == workflow_definition_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound("workflow definition not found")
        row.current_version_id, row.updated_at = version_id, datetime.now(UTC)


class SqlWorkflowVersionRepository:
    """Persist immutable workflow versions and lifecycle status."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, tenant_id: UUID, version_id: UUID) -> WorkflowVersion | None:
        row = (
            await self._s.execute(
                select(VersionRow).where(
                    VersionRow.tenant_id == tenant_id, VersionRow.id == version_id
                )
            )
        ).scalar_one_or_none()
        return workflow_version_to_domain(row) if row else None

    async def add(self, version: WorkflowVersion) -> None:
        self._s.add(VersionRow(**workflow_version_from_domain(version)))

    async def get_latest_for_workflow(
        self, tenant_id: UUID, workflow_definition_id: UUID
    ) -> WorkflowVersion | None:
        row = (
            await self._s.execute(
                select(VersionRow)
                .where(
                    VersionRow.tenant_id == tenant_id,
                    VersionRow.workflow_definition_id == workflow_definition_id,
                )
                .order_by(VersionRow.version_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return workflow_version_to_domain(row) if row else None

    async def list_versions(
        self, tenant_id: UUID, workflow_definition_id: UUID
    ) -> list[WorkflowVersion]:
        """List all versions for a tenant-scoped workflow in number order."""
        rows = (
            (
                await self._s.execute(
                    select(VersionRow)
                    .where(
                        VersionRow.tenant_id == tenant_id,
                        VersionRow.workflow_definition_id == workflow_definition_id,
                    )
                    .order_by(VersionRow.version_number.asc())
                )
            )
            .scalars()
            .all()
        )
        return [workflow_version_to_domain(row) for row in rows]

    async def get_by_version_number(
        self, tenant_id: UUID, workflow_definition_id: UUID, version_number: int
    ) -> WorkflowVersion | None:
        row = (
            await self._s.execute(
                select(VersionRow).where(
                    VersionRow.tenant_id == tenant_id,
                    VersionRow.workflow_definition_id == workflow_definition_id,
                    VersionRow.version_number == version_number,
                )
            )
        ).scalar_one_or_none()
        return workflow_version_to_domain(row) if row else None

    async def mark_status(
        self,
        tenant_id: UUID,
        version_id: UUID,
        *,
        new_status: WorkflowVersionStatus,
        published_at: datetime | None,
        published_by: UUID | None,
    ) -> None:
        row = (
            await self._s.execute(
                select(VersionRow).where(
                    VersionRow.tenant_id == tenant_id, VersionRow.id == version_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound("workflow version not found")
        current = WorkflowVersionStatus(row.status)
        if not (
            (
                current is WorkflowVersionStatus.DRAFT
                and new_status is WorkflowVersionStatus.PUBLISHED
            )
            or (
                current is WorkflowVersionStatus.PUBLISHED
                and new_status is WorkflowVersionStatus.RETIRED
            )
        ):
            raise InvariantViolation(
                f"invalid workflow version transition: {current.value} -> {new_status.value}"
            )
        row.status, row.published_at, row.published_by = (
            new_status.value,
            published_at,
            published_by,
        )


__all__ = ["SqlQueueRepository", "SqlWorkflowDefinitionRepository", "SqlWorkflowVersionRepository"]
