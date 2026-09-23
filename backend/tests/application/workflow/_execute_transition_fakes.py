"""In-memory fakes for the execute_transition tests (P02-US1 / FM-014 / FR-009).

Mirrors the style of ``tests/application/_workflow_fakes.py`` and
``tests/application/cases/_fakes.py``. Each fake captures the exact
shape of the protocol method so the test can assert on every
observable side effect (instance state bump, case workflow-state
update, follow-on tasks, audit rows, outbox enqueues).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.domain.case import Case
from app.domain.errors import Conflict
from app.domain.task import Task
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
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


@dataclass
class StagedOutbox:
    tenant_id: UUID
    event_type: str
    event_id: UUID
    payload: dict[str, Any]


class InMemoryOutboxWriter:
    def __init__(self) -> None:
        self.records: list[StagedOutbox] = []

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        self.records.append(
            StagedOutbox(
                tenant_id=tenant_id,
                event_type=event_type,
                event_id=event_id,
                payload=payload,
            )
        )


class InMemoryCaseRepository:
    """Minimal CaseRepository for transition tests."""

    def __init__(self) -> None:
        self._cases: list[Case] = []
        self.update_calls: int = 0

    def add(self, case: Case) -> None:
        self._cases.append(case)

    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
        for case in self._cases:
            if case.tenant_id == tenant_id and case.id == case_id:
                return case
        return None

    async def update(self, case: Case) -> None:
        """Persist a case with optimistic concurrency.

        Accepts a no-bump write (same ``version_token`` — used by
        :meth:`Case.set_workflow_state` on the initial assignment) and
        a normal bump (``version_token == stored + 1`` — used by
        :meth:`Case.edit`). A token that is older than the stored one
        or that jumps by more than one raises :class:`Conflict` so the
        test exercises the same path as production.
        """
        for idx, existing in enumerate(self._cases):
            if existing.tenant_id == case.tenant_id and existing.id == case.id:
                expected = existing.version_token
                incoming = case.version_token
                if incoming != expected and incoming != expected + 1:
                    raise Conflict(f"stale concurrency token for case {case.id}")
                self._cases[idx] = case
                return
        raise Conflict(f"case {case.id} not found in tenant {case.tenant_id}")


class InMemoryTaskRepository:
    """Minimal TaskRepository for transition tests."""

    def __init__(self) -> None:
        self._tasks: list[Task] = []

    async def add(self, task: Task) -> None:
        self._tasks.append(task)

    async def update(self, task: Task) -> None:
        for idx, existing in enumerate(self._tasks):
            if existing.tenant_id == task.tenant_id and existing.id == task.id:
                self._tasks[idx] = task
                return

    async def get(self, tenant_id: UUID, task_id: UUID) -> Task | None:
        for task in self._tasks:
            if task.tenant_id == tenant_id and task.id == task_id:
                return task
        return None

    async def list_for_case(self, tenant_id: UUID, case_id: UUID) -> list[Task]:
        return [
            task for task in self._tasks if task.tenant_id == tenant_id and task.case_id == case_id
        ]


@dataclass
class StoredInstance:
    id: UUID
    tenant_id: UUID
    case_id: UUID
    workflow_definition_id: UUID
    workflow_version_id: UUID
    current_state: str
    correlation_id: str | None
    version_token: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None


class InMemoryWorkflowInstanceRepository:
    """In-memory WorkflowInstanceRepository.

    Captures every ``bump_state`` call so the test can assert the
    optimistic-concurrency check rejected stale tokens and the post-
    bump shape was correct.
    """

    def __init__(self) -> None:
        self._instances: dict[UUID, StoredInstance] = {}
        self.bump_calls: list[tuple[UUID, str, int]] = []

    def add(self, instance: StoredInstance) -> None:
        self._instances[instance.id] = instance

    async def get(self, tenant_id: UUID, instance_id: UUID) -> StoredInstance | None:
        inst = self._instances.get(instance_id)
        if inst is None or inst.tenant_id != tenant_id:
            return None
        return inst

    async def get_for_case(self, tenant_id: UUID, case_id: UUID) -> StoredInstance | None:
        for inst in self._instances.values():
            if inst.tenant_id == tenant_id and inst.case_id == case_id:
                return inst
        return None

    async def update(self, instance: StoredInstance) -> None:
        self._instances[instance.id] = instance

    async def bump_state(
        self,
        *,
        tenant_id: UUID,
        instance_id: UUID,
        new_state: str,
        expected_version_token: int,
        occurred_at: datetime,
    ) -> StoredInstance:
        inst = self._instances.get(instance_id)
        if inst is None or inst.tenant_id != tenant_id:
            raise LookupError("workflow instance not found")
        if inst.version_token != expected_version_token:
            raise Conflict(f"stale workflow instance token for {instance_id}")
        self.bump_calls.append((instance_id, new_state, expected_version_token))
        bumped = replace(
            inst,
            current_state=new_state,
            updated_at=occurred_at,
            version_token=inst.version_token + 1,
        )
        self._instances[instance_id] = bumped
        return bumped


class InMemoryWorkflowVersionRepository:
    """In-memory WorkflowVersionRepository."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, WorkflowVersion] = {}

    async def add(self, version: WorkflowVersion) -> None:
        self._by_id[version.id] = version

    async def get(self, tenant_id: UUID, version_id: UUID) -> WorkflowVersion | None:
        v = self._by_id.get(version_id)
        if v is None or v.tenant_id != tenant_id:
            return None
        return v


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_published_workflow(
    *,
    tenant_id: UUID,
    transition_keys: tuple[tuple[str, str, str], ...] = (),
    with_actions: bool = False,
) -> tuple[WorkflowDefinition, WorkflowVersion]:
    """Build a minimal published WorkflowDefinition + Version.

    ``transition_keys`` is a sequence of ``(transition_key, from_state,
    to_state)`` triples. When ``with_actions=True`` every transition
    fires a single ``create_task`` action of type ``"follow_on_review"``.
    """
    states = (
        _state("intake", is_initial=True),
        _state("in_review"),
        _state("decided"),
        _state("closed", is_terminal=True),
    )
    transitions = tuple(
        _transition(
            tenant_id=tenant_id,
            key=key,
            from_state=from_state,
            to_state=to_state,
            with_create_task=with_actions,
        )
        for key, from_state, to_state in transition_keys
    )
    definition = WorkflowDefinition.create(
        tenant_id=tenant_id,
        key="ServiceRequestWorkflow",
        name="Service Request Workflow",
    )
    version = WorkflowVersion.draft(
        tenant_id=tenant_id,
        workflow_definition_id=definition.id,
        version_number=1,
        states=states,
        transitions=transitions,
    )
    published = version.publish(
        actor_id=UUID("00000000-0000-0000-0000-000000000099"),
        at=datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC),
    )
    return definition, published


def _state(key: str, *, is_initial: bool = False, is_terminal: bool = False):
    return WorkflowState(key=key, label=key.title(), is_initial=is_initial, is_terminal=is_terminal)


def _transition(
    *,
    tenant_id: UUID,
    key: str,
    from_state: str,
    to_state: str,
    with_create_task: bool,
):
    actions: tuple[dict[str, Any], ...] = ()
    if with_create_task:
        actions = (
            {
                "type": "create_task",
                "task_type": "follow_on_review",
                "title": "Follow-on review",
                "queue": "review_queue",
            },
        )
    return WorkflowTransition(
        key=key,
        from_state=from_state,
        to_state=to_state,
        human_driven=True,
        guard=None,
        actions=actions,
    )


__all__ = [
    "FixedClock",
    "InMemoryAuditStore",
    "InMemoryCaseRepository",
    "InMemoryOutboxWriter",
    "InMemoryTaskRepository",
    "InMemoryWorkflowInstanceRepository",
    "InMemoryWorkflowVersionRepository",
    "StagedAudit",
    "StagedOutbox",
    "StoredInstance",
    "make_published_workflow",
]
