"""Draft a new ``WorkflowVersion`` (FM-013 / FR-015).

Drafting creates a new version row in ``draft`` status. The version
number is monotonic per ``WorkflowDefinition`` (1, 2, 3, …); the
repository reads the latest existing version and increments. The same
workflow key must exist; the draft is created against the existing
header.

# Validation

The definition is validated at draft time too — not only at publish.
This catches unreachable states, ambiguous transitions, invalid guard
operators, and automatic cycles early, and makes the publish path a
short confirmation that the definition is still valid. A definition
that failed validation at draft time is not persisted.

# Why a separate draft endpoint

Pillar P10 §3 calls out the "draft → validate → publish → retire"
lifecycle; the draft step has its own authorization and audit semantics
(``ConfigurationDrafted``). Splitting it from publish keeps the
``POST /admin/workflows`` and
``POST /admin/workflows/{key}/versions`` endpoints composable.
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
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _QueueRepository(Protocol):
    async def list_keys(self, tenant_id: UUID) -> list[str]: ...


class _WorkflowRepo(Protocol):
    async def get_by_key(self, tenant_id: UUID, key: str) -> WorkflowDefinition | None: ...


class _WorkflowVersionRepo(Protocol):
    async def get_latest_for_workflow(
        self, tenant_id: UUID, workflow_definition_id: UUID
    ) -> WorkflowVersion | None: ...

    async def add(self, version: WorkflowVersion) -> None: ...


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
class DraftWorkflowVersionCommand:
    tenant_id: UUID
    actor_id: UUID
    workflow_key: str
    states: tuple[WorkflowState, ...]
    transitions: tuple[WorkflowTransition, ...]
    correlation_id: str


@dataclass(frozen=True, slots=True)
class DraftWorkflowVersionResult:
    workflow_definition_id: UUID
    workflow_version_id: UUID
    version_number: int


async def handle(
    cmd: DraftWorkflowVersionCommand,
    *,
    workflow_repo: _WorkflowRepo,
    workflow_version_repo: _WorkflowVersionRepo,
    queue_repo: _QueueRepository,
    audit_store: _AuditStore,
    clock: _Clock,
) -> DraftWorkflowVersionResult:
    """Create a new draft ``WorkflowVersion``.

    Raises:
        NotFound: the WorkflowDefinition key does not exist for this tenant.
        ValidationFailed: the definition is malformed.
    """
    workflow = await workflow_repo.get_by_key(cmd.tenant_id, cmd.workflow_key)
    if workflow is None:
        raise NotFound(f"workflow definition {cmd.workflow_key!r} not found")

    # Build the in-memory version first so we can run the validator
    # before persisting. The factory's invariants catch structural
    # problems (empty states, bad keys); the validator catches
    # reachability, terminal-state, and cycle problems.
    latest = await workflow_version_repo.get_latest_for_workflow(cmd.tenant_id, workflow.id)
    next_version = (latest.version_number + 1) if latest is not None else 1

    now = clock.now()
    draft = WorkflowVersion.draft(
        tenant_id=cmd.tenant_id,
        workflow_definition_id=workflow.id,
        version_number=next_version,
        states=cmd.states,
        transitions=cmd.transitions,
        now=now,
    )
    queue_keys = await queue_repo.list_keys(cmd.tenant_id)
    validate_definition(draft, known_queues=queue_keys)

    await workflow_version_repo.add(draft)

    await audit_store.append(
        tenant_id=cmd.tenant_id,
        event_type="ConfigurationDrafted",
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="WorkflowVersion",
        subject_id=draft.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "workflow_definition_id": str(workflow.id),
            "workflow_key": workflow.key,
            "version_number": draft.version_number,
            "state_keys": list(draft.state_keys()),
            "transition_keys": list(draft.transition_keys()),
        },
        occurred_at=now,
    )

    return DraftWorkflowVersionResult(
        workflow_definition_id=workflow.id,
        workflow_version_id=draft.id,
        version_number=draft.version_number,
    )


__all__ = [
    "DraftWorkflowVersionCommand",
    "DraftWorkflowVersionResult",
    "handle",
]
