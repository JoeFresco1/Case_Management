"""Start a workflow instance for a new case (P02-US1 / FM-014 / T070).

Starting a workflow means:

1. Picking the published ``WorkflowVersion`` the case will pin
   against (``WorkflowDefinition.current_version_id``).
2. Creating a ``WorkflowInstance`` row whose ``current_state`` is the
   workflow's initial state.
3. Materializing the configured initial tasks (``actions`` of type
   ``create_task`` on transitions out of the initial state — but only
   those that should fire as "welcome" tasks at startup; the engine
   does not invoke the transition here).
4. Writing the ``WorkflowStarted`` audit + outbox rows in the same
   transaction so the instance pointer, the initial tasks, the audit
   row, and the outbox event commit together (Constitution III).

A ``WorkflowInstance`` is **pinned** to a published
``workflow_version_id``. When a new version is published, existing
instances continue to execute against the version they were started
on (P02 pillar §2 — running cases remain pinned). The instance row
carries both ``workflow_definition_id`` and ``workflow_version_id``
so the engine can read either directly.

# INTEGRATION_SEAM: WorkflowInstanceRepository

``app.domain.ports.WorkflowInstanceRepository`` is the persistence
Protocol. The first-slice SQL adapter lands alongside the workflow
adapters in a later slice (T063 / T071); the use case here depends on
the Protocol only.

# Fail-closed

If the WorkflowDefinition has no published version the use case
raises :class:`NotFound`. The create-case path is wired so the caller
sees a structured error, never an instance pointing at a draft.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.domain.errors import NotFound
from app.domain.ports import (
    AuditEventStore,
    OutboxWriter,
    SlaClockRepository,
    TaskRepository,
    WorkflowInstanceRepository,
    WorkflowVersionRepository,
)
from app.domain.sla import SlaClock
from app.domain.task import Task
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowVersion,
    WorkflowVersionStatus,
)


class _ClockLike(Protocol):
    """Minimal clock protocol used by :func:`handle`."""

    def now(self) -> datetime: ...


class _WorkflowDefinitionRepositoryLike(Protocol):
    """The minimal WorkflowDefinitionRepository surface used here.

    Defined as a local Protocol so the use case does not have to
    import the full ``ports`` module's Protocol hierarchy in tests
    that swap in a fake.
    """

    async def get(
        self, tenant_id: UUID, workflow_definition_id: UUID
    ) -> WorkflowDefinition | None: ...

    async def get_by_key(self, tenant_id: UUID, key: str) -> WorkflowDefinition | None: ...


@dataclass(frozen=True, slots=True)
class WorkflowInstance:
    """A live execution of a workflow version for one case.

    Lightweight value object — the persistence row mirrors these
    fields. The aggregate carries ``version_token`` so a concurrent
    transition (executed by ``execute_transition.handle``) is refused
    with :class:`Conflict` when the stored token differs (Constitution
    V).
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    workflow_definition_id: UUID
    workflow_version_id: UUID
    current_state: str
    correlation_id: str | None = None
    version_token: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None


# Subject / event-type constants — stable strings; subscribers depend
# on them.
SUBJECT_TYPE_WORKFLOW_INSTANCE = "workflow_instance"
EVENT_WORKFLOW_STARTED = "WorkflowStarted"

# Task types configured as "welcome" tasks in v1. Real configurations
# land with workflow definitions; the constant is here so the v1
# tests share the same identifier.
WELCOME_TRANSITION_KEY = "initial"


@dataclass(frozen=True, slots=True)
class StartWorkflowCommand:
    """Input to :func:`handle`.

    ``workflow_key`` is the stable ``key`` of the WorkflowDefinition
    (e.g., ``"ServiceRequestWorkflow"``). The use case resolves the
    definition by key, picks the current published version, and
    pins the instance against it.
    """

    tenant_id: UUID
    actor_id: UUID
    case_id: UUID
    workflow_key: str
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class StartWorkflowResult:
    """Post-commit outcome returned to the router / caller.

    ``instance`` is the freshly created WorkflowInstance. ``tasks`` is
    the list of initial tasks materialized from the workflow version
    (the welcome tasks that the worker should pick up immediately).
    """

    instance: WorkflowInstance
    tasks: list[Task] = field(default_factory=list)


async def handle(
    cmd: StartWorkflowCommand,
    *,
    workflow_repo: _WorkflowDefinitionRepositoryLike,
    workflow_version_repo: WorkflowVersionRepository,
    instance_repo: WorkflowInstanceRepository,
    task_repo: TaskRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _ClockLike,
    sla_clock_repo: SlaClockRepository | None = None,
) -> StartWorkflowResult:
    """Start a workflow instance for ``case_id`` with welcome tasks.

    Failure behavior:

    - Missing or unpublished ``workflow_key`` raises ``NotFound``.
    - The instance row, the welcome task rows, the audit row, and the
      outbox row share one transaction; the caller commits once.
    """
    definition = await workflow_repo.get_by_key(cmd.tenant_id, cmd.workflow_key)
    if definition is None or definition.current_version_id is None:
        raise NotFound(f"workflow {cmd.workflow_key!r} not found or has no published version")

    version = await workflow_version_repo.get(cmd.tenant_id, definition.current_version_id)
    if version is None or version.status is not WorkflowVersionStatus.PUBLISHED:
        # Defense-in-depth: the WorkflowDefinition claimed a
        # ``current_version_id`` but the row is missing or retired.
        raise NotFound(f"workflow {cmd.workflow_key!r} has no usable published version")

    initial_state = _initial_state(version)
    now = clock.now()

    instance = WorkflowInstance(
        id=uuid4(),
        tenant_id=cmd.tenant_id,
        case_id=cmd.case_id,
        workflow_definition_id=definition.id,
        workflow_version_id=version.id,
        current_state=initial_state,
        correlation_id=cmd.correlation_id or None,
        version_token=1,
        created_at=now,
        updated_at=now,
    )
    await instance_repo.add(instance)

    # Materialize the welcome tasks. v1 picks every transition out of
    # the initial state whose actions declare a ``create_task`` and
    # stages them as ``open`` Tasks on the case. The first slice
    # deliberately ignores ``set_field`` and ``start_sla`` actions at
    # start-up time — those are transition-time effects and land with
    # ``execute_transition`` (T071).
    tasks: list[Task] = []
    sla_actions: list[dict[str, Any]] = []
    for transition in version.outgoing(initial_state):
        for action in transition.actions:
            if action.get("type") == "start_sla":
                sla_actions.append(action)
                continue
            if action.get("type") != "create_task":
                continue
            task_type = str(action.get("task_type", "")).strip()
            if not task_type:
                # The publish-time validator rejects an action without
                # a task_type; runtime is defense-in-depth only.
                continue
            task = Task.create(
                tenant_id=cmd.tenant_id,
                case_id=cmd.case_id,
                type=task_type,
                title=str(action.get("title", task_type)),
                priority=str(action.get("priority", "normal")),
                assignee_queue_id=_as_queue(action.get("queue")),
                assignee_user_id=_as_uuid(action.get("assignee_user_id")),
                due_at=None,
                created_at=now,
                created_by_transition=WELCOME_TRANSITION_KEY,
            )
            tasks.append(task)

    for task in tasks:
        await task_repo.add(task)

    if sla_actions and sla_clock_repo is not None:
        await _start_sla_clocks(
            sla_actions,
            tenant_id=cmd.tenant_id,
            case_id=cmd.case_id,
            now=now,
            sla_clock_repo=sla_clock_repo,
        )

    correlation_id = cmd.correlation_id or _new_correlation_id()

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_WORKFLOW_STARTED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_WORKFLOW_INSTANCE,
        subject_id=instance.id,
        case_id=cmd.case_id,
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            "case_id": str(cmd.case_id),
            "workflow_key": cmd.workflow_key,
            "workflow_version_id": str(version.id),
            "initial_state": initial_state,
            "task_count": len(tasks),
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_WORKFLOW_STARTED,
        event_id=uuid4(),
        payload={
            "workflow_instance_id": str(instance.id),
            "case_id": str(cmd.case_id),
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor_id),
            "workflow_key": cmd.workflow_key,
            "workflow_version_id": str(version.id),
            "initial_state": initial_state,
            "occurred_at": now.isoformat(),
        },
    )

    return StartWorkflowResult(instance=instance, tasks=tasks)


# ----- helpers --------------------------------------------------------------


async def _start_sla_clocks(
    sla_actions: list[dict[str, Any]],
    *,
    tenant_id: UUID,
    case_id: UUID,
    now: datetime,
    sla_clock_repo: SlaClockRepository,
) -> None:
    """Start one SLA clock per ``start_sla`` action (FM-018 runtime half).

    Malformed actions are skipped rather than creating an invalid
    deadline — the publish-time validator is the enforcement point;
    runtime here is defense-in-depth only.
    """
    for action in sla_actions:
        clock_key = str(action.get("sla_key", "")).strip()
        hours = _positive_hours(action.get("hours"))
        if not clock_key or hours is None:
            # why: malformed actions should be rejected at publish time;
            # runtime skips them rather than creating an invalid deadline.
            continue
        await sla_clock_repo.add(
            SlaClock.start(
                tenant_id=tenant_id,
                case_id=case_id,
                clock_key=clock_key,
                started_at=now,
                due_at=now + timedelta(hours=hours),
                escalation_on_breach=_as_escalation(
                    action.get("escalation_on_breach", action.get("escalation"))
                ),
                escalation_on_warning=_as_escalation(action.get("escalation_on_warning")),
            )
        )


def _as_escalation(value: Any) -> dict[str, Any] | None:
    """Coerce a raw escalation payload to a dict, or ``None`` when absent/invalid."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return None


def _initial_state(version: WorkflowVersion) -> str:
    """Return the workflow's initial state key (the only one)."""
    for state in version.states:
        if state.is_initial:
            return state.key
    # The publish-time validator guarantees exactly one initial state;
    # runtime is defense-in-depth.
    raise NotFound("workflow version has no initial state")


def _as_queue(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def _as_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _positive_hours(value: Any) -> float | None:
    """Parse a positive workflow SLA duration, or return ``None``."""
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    return hours if hours > 0 else None


def _new_correlation_id() -> str:
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


__all__ = [
    "EVENT_WORKFLOW_STARTED",
    "StartWorkflowCommand",
    "StartWorkflowResult",
    "SUBJECT_TYPE_WORKFLOW_INSTANCE",
    "WorkflowInstance",
    "handle",
]
