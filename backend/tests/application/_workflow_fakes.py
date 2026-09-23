"""In-memory fakes for the workflow configuration lifecycle tests.

Mirrors the style of ``tests/application/cases/_fakes.py`` and
``tests/application/_publish_case_type_fakes.py``. Each fake captures
the exact shape of the protocol method so the test can assert every
observable side effect (status flip, current-version pointer, audit
row, draft add).

Why in-memory fakes: AGENTS.md §4.R5 prefers fakes over a live
container. The workflow use cases are pure orchestration functions;
the only side effects they have are "stage a row", "flip the row's
status", and "append audit". Fakes capture the same shape and let
the test assert exactly what the use case committed.

# Two tenants

The fixture constants in the tests expose ACME_TENANT and BETA_TENANT
so the tenant-isolation test can exercise a cross-tenant publish
attempt. The fakes always scope by ``tenant_id`` first.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.domain.errors import InvariantViolation
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowVersion,
    WorkflowVersionStatus,
)


class FixedClock:
    """A ``Clock`` Protocol implementation that returns a fixed UTC instant."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


@dataclass
class StagedAudit:
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    actor_type: str
    subject_type: str
    subject_id: UUID
    case_id: UUID | None
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    occurred_at: datetime


class InMemoryAuditStore:
    """In-memory ``AuditEventStore``.

    Captures every ``append`` call so the test can assert the audit row
    landed in the same transaction as the version status flip.
    """

    def __init__(self) -> None:
        self.events: list[StagedAudit] = []

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
    ) -> None:
        self.events.append(
            StagedAudit(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                actor_type=actor_type,
                subject_type=subject_type,
                subject_id=subject_id,
                case_id=case_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        )


class InMemoryWorkflowDefinitionRepository:
    """In-memory ``WorkflowDefinitionRepository``.

    Captures every ``set_current_version`` call so the test can assert
    the publish flow promoted the right version in the right order.
    """

    def __init__(self) -> None:
        self._by_id: dict[UUID, WorkflowDefinition] = {}
        self._by_key: dict[tuple[UUID, str], WorkflowDefinition] = {}
        self.set_current_version_calls: list[tuple[UUID, UUID, UUID]] = []

    async def add(self, workflow: WorkflowDefinition) -> None:
        self._by_id[workflow.id] = workflow
        self._by_key[(workflow.tenant_id, workflow.key)] = workflow

    async def get(self, tenant_id: UUID, workflow_definition_id: UUID) -> WorkflowDefinition | None:
        wf = self._by_id.get(workflow_definition_id)
        if wf is None or wf.tenant_id != tenant_id:
            return None
        return wf

    async def get_by_key(self, tenant_id: UUID, key: str) -> WorkflowDefinition | None:
        return self._by_key.get((tenant_id, key))

    async def set_current_version(
        self, tenant_id: UUID, workflow_definition_id: UUID, version_id: UUID
    ) -> None:
        wf = self._by_id.get(workflow_definition_id)
        if wf is None or wf.tenant_id != tenant_id:
            raise LookupError("workflow definition not found")
        self._by_id[workflow_definition_id] = replace(wf, current_version_id=version_id)
        self._by_key[(tenant_id, wf.key)] = self._by_id[workflow_definition_id]
        self.set_current_version_calls.append((tenant_id, workflow_definition_id, version_id))


class InMemoryWorkflowVersionRepository:
    """In-memory ``WorkflowVersionRepository``.

    Mirrors the case-type version repo's invariants: only
    ``draft → published`` is allowed through ``mark_status``; the
    domain-level ``publish`` / ``retire`` are the primary guards, this
    method is defense-in-depth.
    """

    def __init__(self) -> None:
        self._by_id: dict[UUID, WorkflowVersion] = {}
        self.status_transitions: list[
            tuple[UUID, WorkflowVersionStatus, WorkflowVersionStatus]
        ] = []

    async def add(self, version: WorkflowVersion) -> None:
        self._by_id[version.id] = version

    async def get(self, tenant_id: UUID, version_id: UUID) -> WorkflowVersion | None:
        v = self._by_id.get(version_id)
        if v is None or v.tenant_id != tenant_id:
            return None
        return v

    async def get_latest_for_workflow(
        self, tenant_id: UUID, workflow_definition_id: UUID
    ) -> WorkflowVersion | None:
        candidates = [
            v
            for v in self._by_id.values()
            if v.tenant_id == tenant_id and v.workflow_definition_id == workflow_definition_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda v: v.version_number)

    async def get_by_version_number(
        self, tenant_id: UUID, workflow_definition_id: UUID, version_number: int
    ) -> WorkflowVersion | None:
        for v in self._by_id.values():
            if (
                v.tenant_id == tenant_id
                and v.workflow_definition_id == workflow_definition_id
                and v.version_number == version_number
            ):
                return v
        return None

    async def mark_status(
        self,
        tenant_id: UUID,
        version_id: UUID,
        *,
        new_status: WorkflowVersionStatus,
        published_at: datetime | None,
        published_by: UUID | None,
    ) -> None:
        v = self._by_id.get(version_id)
        if v is None or v.tenant_id != tenant_id:
            raise LookupError("workflow version not found")
        current = v.status
        # Defense-in-depth: refuse anything other than draft → published
        # at this layer too. ``WorkflowVersion.publish`` already raises
        # on the domain side; this mirrors the guard.
        if current is not WorkflowVersionStatus.DRAFT:
            raise InvariantViolation(f"cannot update a {current.value} workflow version")
        if new_status is not WorkflowVersionStatus.PUBLISHED:
            raise InvariantViolation(f"unexpected transition to {new_status.value}")
        self.status_transitions.append((version_id, current, new_status))
        self._by_id[version_id] = replace(
            v,
            status=new_status,
            published_at=published_at,
            published_by=published_by,
        )


class InMemoryQueueRepository:
    """In-memory ``QueueRepository``.

    Holds the set of queue keys the workflow may reference. Tests that
    need a non-empty catalog seed the ``keys`` list explicitly; the
    default empty set is fail-closed — any action reference is rejected.
    """

    def __init__(self, *, keys: tuple[str, ...] = ()) -> None:
        self._keys = set(keys)

    def add(self, key: str) -> None:
        self._keys.add(key)

    async def list_keys(self, tenant_id: UUID) -> list[str]:
        # The fake ignores ``tenant_id`` because tests run a single
        # tenant at a time. A real adapter would scope by tenant.
        _ = tenant_id
        return sorted(self._keys)


def make_workflow_definition(
    *,
    tenant_id: UUID,
    key: str = "ServiceRequestWorkflow",
    name: str = "Service Request Workflow",
) -> WorkflowDefinition:
    """Build a fresh ``WorkflowDefinition`` header (no versions yet)."""
    return WorkflowDefinition.create(
        tenant_id=tenant_id,
        key=key,
        name=name,
        description="Synthetic workflow definition for tests.",
    )


__all__ = [
    "FixedClock",
    "InMemoryAuditStore",
    "InMemoryQueueRepository",
    "InMemoryWorkflowDefinitionRepository",
    "InMemoryWorkflowVersionRepository",
    "StagedAudit",
    "make_workflow_definition",
]
