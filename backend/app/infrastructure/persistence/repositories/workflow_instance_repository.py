"""Tenant-scoped SQL persistence for runtime workflow instances."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.workflow.start_workflow import WorkflowInstance
from app.domain.errors import Conflict
from app.domain.workflow import WorkflowTransitionRecord
from app.infrastructure.persistence.models import WorkflowInstance as InstanceRow
from app.infrastructure.persistence.models import WorkflowTransition as TransitionRow


def _domain(row: InstanceRow) -> WorkflowInstance:
    """Map an ORM row to the application workflow value object."""
    return WorkflowInstance(
        id=row.id,
        tenant_id=row.tenant_id,
        case_id=row.case_id,
        workflow_definition_id=row.workflow_definition_id,
        workflow_version_id=row.workflow_version_id,
        current_state=row.current_state,
        correlation_id=row.correlation_id,
        version_token=row.version_token,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlWorkflowInstanceRepository:
    """Persist and compare-and-swap workflow state pointers."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, instance: WorkflowInstance) -> None:
        self._s.add(
            InstanceRow(
                id=instance.id,
                tenant_id=instance.tenant_id,
                case_id=instance.case_id,
                workflow_definition_id=instance.workflow_definition_id,
                workflow_version_id=instance.workflow_version_id,
                current_state=instance.current_state,
                correlation_id=instance.correlation_id or "",
                version_token=instance.version_token,
                created_at=instance.created_at or datetime.now(UTC),
                updated_at=instance.updated_at or datetime.now(UTC),
            )
        )

    async def get(self, tenant_id: UUID, instance_id: UUID) -> WorkflowInstance | None:
        row = (
            await self._s.execute(
                select(InstanceRow).where(
                    InstanceRow.tenant_id == tenant_id, InstanceRow.id == instance_id
                )
            )
        ).scalar_one_or_none()
        return _domain(row) if row else None

    async def get_for_case(self, tenant_id: UUID, case_id: UUID) -> WorkflowInstance | None:
        row = (
            await self._s.execute(
                select(InstanceRow).where(
                    InstanceRow.tenant_id == tenant_id, InstanceRow.case_id == case_id
                )
            )
        ).scalar_one_or_none()
        return _domain(row) if row else None

    async def update(self, instance: WorkflowInstance) -> None:
        result = await self._s.execute(
            update(InstanceRow)
            .where(
                InstanceRow.tenant_id == instance.tenant_id,
                InstanceRow.id == instance.id,
                InstanceRow.version_token == instance.version_token - 1,
            )
            .values(
                current_state=instance.current_state,
                updated_at=instance.updated_at,
                version_token=instance.version_token,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount == 0:
            raise Conflict(f"stale concurrency token for workflow instance {instance.id}")

    async def bump_state(
        self,
        *,
        tenant_id: UUID,
        instance_id: UUID,
        new_state: str,
        expected_version_token: int,
        occurred_at: datetime,
    ) -> WorkflowInstance:
        result = await self._s.execute(
            update(InstanceRow)
            .where(
                InstanceRow.tenant_id == tenant_id,
                InstanceRow.id == instance_id,
                InstanceRow.version_token == expected_version_token,
            )
            .values(
                current_state=new_state,
                updated_at=occurred_at,
                version_token=expected_version_token + 1,
            )
        )
        if not isinstance(result, CursorResult) or result.rowcount == 0:
            raise Conflict(f"stale concurrency token for workflow instance {instance_id}")
        row = (
            await self._s.execute(
                select(InstanceRow).where(
                    InstanceRow.tenant_id == tenant_id, InstanceRow.id == instance_id
                )
            )
        ).scalar_one()
        return _domain(row)


class SqlWorkflowTransitionLog:
    """Append-only log of executed workflow transitions (T069)."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(self, entry: WorkflowTransitionRecord) -> None:
        """Stage one transition record on the caller's transaction."""
        self._s.add(
            TransitionRow(
                id=entry.id,
                tenant_id=entry.tenant_id,
                workflow_instance_id=entry.workflow_instance_id,
                from_state=entry.from_state,
                to_state=entry.to_state,
                transition_key=entry.transition_key,
                actor_id=entry.actor_id,
                occurred_at=entry.occurred_at,
                payload=dict(entry.payload),
                correlation_id=entry.correlation_id,
                causation_id=entry.causation_id,
            )
        )


__all__ = ["SqlWorkflowInstanceRepository", "SqlWorkflowTransitionLog"]
