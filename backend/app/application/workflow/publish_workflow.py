"""Publish a draft ``WorkflowVersion`` (FM-013 / FR-015).

This use case orchestrates the ``draft → published`` transition for a
workflow definition. It mirrors :mod:`app.application.configuration.publish_case_type`
so the operator-facing API has consistent semantics across
configuration artifacts.

1. Look up the draft version by id (tenant-scoped). Missing -> ``NotFound``.
2. Run the validator over the version's ``states`` + ``transitions``
   using the tenant's currently-known queues. A defect raises
   ``ValidationFailed`` and the transaction aborts.
3. Look up the owning ``WorkflowDefinition``. Missing -> ``NotFound``.
4. Re-issue the domain entity via ``WorkflowVersion.publish`` so the
   ``published_at`` / ``published_by`` fields are stamped by the Clock.
5. Persist the new status, set the WorkflowDefinition's
   ``current_version_id`` to the published version, and emit a
   ``ConfigurationPublished`` audit event.

The whole flow runs inside one DB transaction so the status flip, the
current-version pointer, and the audit row are atomic (Constitution
III / plan §4.R12). A failure anywhere rolls back every side effect.

# Why not in the domain

The publish lifecycle crosses multiple aggregates (WorkflowDefinition
+ WorkflowVersion + AuditEvent) and writes through ports the domain
does not own (audit, time, queues). The orchestration belongs in the
application layer; the domain owns the invariants on ``publish()``
itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.application.configuration.validate_workflow_definition import (
    validate_definition,
)
from app.domain.errors import NotFound
from app.domain.workflow import WorkflowDefinition, WorkflowVersion, WorkflowVersionStatus


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _QueueRepository(Protocol):
    async def list_keys(self, tenant_id: UUID) -> list[str]: ...


class _WorkflowRepo(Protocol):
    async def get(
        self, tenant_id: UUID, workflow_definition_id: UUID
    ) -> WorkflowDefinition | None: ...
    async def get_by_key(self, tenant_id: UUID, key: str) -> WorkflowDefinition | None: ...
    async def set_current_version(
        self, tenant_id: UUID, workflow_definition_id: UUID, version_id: UUID
    ) -> None: ...


class _WorkflowVersionRepo(Protocol):
    async def get(self, tenant_id: UUID, version_id: UUID) -> WorkflowVersion | None: ...

    async def mark_status(
        self,
        tenant_id: UUID,
        version_id: UUID,
        *,
        new_status: WorkflowVersionStatus,
        published_at: datetime | None,
        published_by: UUID | None,
    ) -> None: ...


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
class PublishWorkflowVersionCommand:
    tenant_id: UUID
    actor_id: UUID
    workflow_version_id: UUID
    correlation_id: str


@dataclass(frozen=True, slots=True)
class PublishWorkflowVersionResult:
    workflow_definition_id: UUID
    workflow_version_id: UUID
    version_number: int
    status: WorkflowVersionStatus


async def handle(
    cmd: PublishWorkflowVersionCommand,
    *,
    workflow_repo: _WorkflowRepo,
    workflow_version_repo: _WorkflowVersionRepo,
    queue_repo: _QueueRepository,
    audit_store: _AuditStore,
    clock: _Clock,
) -> PublishWorkflowVersionResult:
    """Publish a draft ``WorkflowVersion``.

    Raises:
        NotFound: the version (or its owning WorkflowDefinition) does
            not exist.
        InvariantViolation: the version is not in ``draft`` status.
        ValidationFailed: the definition has unresolved defects.
    """
    version = await workflow_version_repo.get(cmd.tenant_id, cmd.workflow_version_id)
    if version is None:
        raise NotFound(f"workflow version {cmd.workflow_version_id} not found")

    workflow = await workflow_repo.get(cmd.tenant_id, version.workflow_definition_id)
    if workflow is None:
        raise NotFound(f"workflow definition {version.workflow_definition_id} not found")

    # A replay of the same publish command is a read-only success. This
    # preserves the immutable version and avoids duplicate audit events.
    if version.status is WorkflowVersionStatus.PUBLISHED:
        return PublishWorkflowVersionResult(
            workflow_definition_id=workflow.id,
            workflow_version_id=version.id,
            version_number=version.version_number,
            status=version.status,
        )

    # Validator runs first; a broken definition never touches the
    # persistence layer (the validator is a pure function over the
    # domain entity).
    queue_keys = await queue_repo.list_keys(cmd.tenant_id)
    validate_definition(version, known_queues=queue_keys)

    now = clock.now()
    published = version.publish(actor_id=cmd.actor_id, at=now)

    await workflow_version_repo.mark_status(
        cmd.tenant_id,
        published.id,
        new_status=WorkflowVersionStatus.PUBLISHED,
        published_at=published.published_at,
        published_by=published.published_by,
    )
    await workflow_repo.set_current_version(cmd.tenant_id, workflow.id, published.id)

    await audit_store.append(
        tenant_id=cmd.tenant_id,
        event_type="ConfigurationPublished",
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="WorkflowVersion",
        subject_id=published.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "workflow_definition_id": str(workflow.id),
            "workflow_key": workflow.key,
            "version_number": published.version_number,
            "state_keys": list(published.state_keys()),
            "transition_keys": list(published.transition_keys()),
        },
        occurred_at=now,
    )

    return PublishWorkflowVersionResult(
        workflow_definition_id=workflow.id,
        workflow_version_id=published.id,
        version_number=published.version_number,
        status=WorkflowVersionStatus.PUBLISHED,
    )


__all__ = [
    "PublishWorkflowVersionCommand",
    "PublishWorkflowVersionResult",
    "handle",
]
