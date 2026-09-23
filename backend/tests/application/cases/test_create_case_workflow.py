"""Workflow start on intake (P02-US1 / FM-014 / T070).

`create_case.handle` accepts an optional `WorkflowStarter` bundle. When a
published workflow definition exists under the `{case_type_key}Workflow`
convention key, intake starts the workflow in the same transaction
(instance + welcome tasks + SLA clocks + `WorkflowStarted` audit/outbox)
and the case carries the initial state. Without a bundle — or without a
matching definition — the case is created workflow-less.

Naming follows `test_<unit>_<scenario>__<expectation>` per AGENTS.md
§4.R5. Workflow fakes are local to this file: the shared
`tests/application/_workflow_fakes.py` covers definition/version/audit,
but instance/task/SLA fakes live with the transition tests in an
incompatible shape (sync `add`, `StoredInstance` rows), so this file
stages the `start_workflow` value objects directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.application.cases.create_case import (
    CreateCaseCommand,
    WorkflowStarter,
    workflow_key_for_case_type,
)
from app.application.cases.create_case import (
    handle as create_case,
)
from app.application.workflow import start_workflow as start_workflow_uc
from app.domain.sla import SlaClock
from app.domain.task import Task
from app.domain.workflow import WorkflowState, WorkflowTransition, WorkflowVersion
from tests.application._workflow_fakes import (
    InMemoryWorkflowDefinitionRepository,
    InMemoryWorkflowVersionRepository,
    make_workflow_definition,
)

from ._fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
    InMemoryOutbox,
    make_case_type,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
INTAKE_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


class _InstanceStore:
    """Stages `start_workflow.WorkflowInstance` value objects."""

    def __init__(self) -> None:
        self.instances: list[start_workflow_uc.WorkflowInstance] = []

    async def add(self, instance: start_workflow_uc.WorkflowInstance) -> None:
        self.instances.append(instance)


class _TaskStore:
    """Stages domain `Task` aggregates."""

    def __init__(self) -> None:
        self.tasks: list[Task] = []

    async def add(self, task: Task) -> None:
        self.tasks.append(task)


class _SlaStore:
    """Stages domain `SlaClock` aggregates."""

    def __init__(self) -> None:
        self.clocks: list[SlaClock] = []

    async def add(self, clock: SlaClock) -> None:
        self.clocks.append(clock)


def _seed_case_repos() -> tuple[
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
]:
    """Build a tenant + published `ServiceRequest` case type."""
    repo = InMemoryCaseRepository()
    repo.set_tenant_short(ACME_TENANT, "ACME")
    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()
    case_type, version = make_case_type(
        tenant_id=ACME_TENANT,
        required_keys=("title", "summary"),
    )
    case_type_repo.add(case_type)
    version_repo.add(version)
    return repo, case_type_repo, version_repo


def _seed_workflow_repos() -> tuple[
    InMemoryWorkflowDefinitionRepository,
    InMemoryWorkflowVersionRepository,
    _InstanceStore,
    _TaskStore,
    _SlaStore,
    Any,
    WorkflowVersion,
]:
    """Build a published `ServiceRequestWorkflow` with welcome actions."""
    definitions = InMemoryWorkflowDefinitionRepository()
    versions = InMemoryWorkflowVersionRepository()
    definition = make_workflow_definition(tenant_id=ACME_TENANT)
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=definition.id,
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="in_review", label="In review"),
            WorkflowState(key="decided", label="Decided"),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="submit_for_review",
                from_state="intake",
                to_state="in_review",
                human_driven=True,
                actions=(
                    {
                        "type": "create_task",
                        "task_type": "review",
                        "title": "Review service request",
                        "queue": "review_queue",
                        "priority": "normal",
                    },
                    {
                        "type": "start_sla",
                        "sla_key": "review_due",
                        "hours": 48,
                        "queue": "review_queue",
                    },
                ),
            ),
            WorkflowTransition(
                key="record_decision",
                from_state="in_review",
                to_state="decided",
                human_driven=True,
            ),
            WorkflowTransition(
                key="close",
                from_state="decided",
                to_state="closed",
                human_driven=True,
            ),
        ),
    )
    return definitions, versions, _InstanceStore(), _TaskStore(), _SlaStore(), definition, version


async def _publish_workflow(
    definitions: InMemoryWorkflowDefinitionRepository,
    versions: InMemoryWorkflowVersionRepository,
    definition: Any,
    version: WorkflowVersion,
) -> None:
    """Stage a published definition + version pair in the fakes."""
    await definitions.add(definition)
    published = version.publish(actor_id=INTAKE_USER, at=FIXED_NOW)
    await versions.add(published)
    await definitions.set_current_version(ACME_TENANT, definition.id, published.id)


def _base_cmd(**overrides: Any) -> CreateCaseCommand:
    payload: dict[str, Any] = {
        "tenant_id": ACME_TENANT,
        "actor_id": INTAKE_USER,
        "actor_roles": ("intake",),
        "case_type_key": "ServiceRequest",
        "title": "Help with onboarding",
        "description": "Need help getting set up.",
        "data": {"title": "Help with onboarding", "summary": "first-time customer"},
        "priority": "normal",
        "correlation_id": "corr-wf-001",
    }
    payload.update(overrides)
    return CreateCaseCommand(**payload)


def _bundle(
    definitions: InMemoryWorkflowDefinitionRepository,
    versions: InMemoryWorkflowVersionRepository,
    instances: _InstanceStore,
    tasks: _TaskStore,
    sla: _SlaStore,
) -> WorkflowStarter:
    return WorkflowStarter(
        workflow_repo=definitions,
        workflow_version_repo=versions,
        instance_repo=instances,  # type: ignore[arg-type]
        task_repo=tasks,  # type: ignore[arg-type]
        sla_clock_repo=sla,  # type: ignore[arg-type]
    )


async def test_create_case__with_workflow__starts_instance_tasks_and_sets_state() -> None:
    """Intake with a matching published workflow starts it atomically."""
    # // given
    case_repo, case_type_repo, version_repo = _seed_case_repos()
    defs, vers, instances, tasks, sla, definition, version = _seed_workflow_repos()
    await _publish_workflow(defs, vers, definition, version)
    audit, outbox = InMemoryAuditStore(), InMemoryOutbox()

    # // when
    case = await create_case(
        _base_cmd(),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit=audit,
        outbox=outbox,
        clock=FixedClock(FIXED_NOW),
        workflow=_bundle(defs, vers, instances, tasks, sla),
    )

    # // then: case carries the initial state with a creation token
    assert case.current_workflow_state == "intake"
    assert case.version_token == 1
    # // and: one pinned instance, one welcome task, one SLA clock
    assert len(instances.instances) == 1
    assert instances.instances[0].case_id == case.id
    assert instances.instances[0].current_state == "intake"
    assert [t.type for t in tasks.tasks] == ["review"]
    assert tasks.tasks[0].assignee_queue_id == "review_queue"
    assert len(sla.clocks) == 1
    assert sla.clocks[0].case_id == case.id
    # // and: audit + outbox hold both the creation and the start events
    assert [e.event_type for e in audit.events] == ["CaseCreated", "WorkflowStarted"]
    assert {e.event_type for e in outbox.events} == {"CaseCreated", "WorkflowStarted"}


async def test_create_case__without_matching_workflow__creates_case_without_instance() -> None:
    """A case type with no workflow definition still creates cases."""
    # // given: bundle wired, but no definition seeded
    case_repo, case_type_repo, version_repo = _seed_case_repos()
    defs, vers, instances, tasks, sla, _definition, _version = _seed_workflow_repos()
    audit, outbox = InMemoryAuditStore(), InMemoryOutbox()

    # // when
    case = await create_case(
        _base_cmd(),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit=audit,
        outbox=outbox,
        clock=FixedClock(FIXED_NOW),
        workflow=_bundle(defs, vers, instances, tasks, sla),
    )

    # // then
    assert case.current_workflow_state is None
    assert instances.instances == []
    assert tasks.tasks == []
    assert sla.clocks == []
    assert [e.event_type for e in audit.events] == ["CaseCreated"]


async def test_create_case__without_workflow_bundle__skips_workflow_start() -> None:
    """Omitting the bundle preserves the validation-only intake path."""
    # // given
    case_repo, case_type_repo, version_repo = _seed_case_repos()
    audit, outbox = InMemoryAuditStore(), InMemoryOutbox()

    # // when
    case = await create_case(
        _base_cmd(),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        audit=audit,
        outbox=outbox,
        clock=FixedClock(FIXED_NOW),
    )

    # // then
    assert case.current_workflow_state is None
    assert [e.event_type for e in audit.events] == ["CaseCreated"]


def test_workflow_key_for_case_type__appends_workflow_suffix() -> None:
    """The v1 convention maps `ServiceRequest` to `ServiceRequestWorkflow`."""
    assert workflow_key_for_case_type("ServiceRequest") == "ServiceRequestWorkflow"
