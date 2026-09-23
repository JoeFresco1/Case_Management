"""Focused acceptance tests for workflow definition creation/publication."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.application.workflow import create_workflow, publish_workflow
from app.domain.workflow import WorkflowState, WorkflowTransition, WorkflowVersion
from tests.application._workflow_fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryQueueRepository,
    InMemoryWorkflowDefinitionRepository,
    InMemoryWorkflowVersionRepository,
    make_workflow_definition,
)

TENANT = uuid4()
ACTOR = uuid4()


def _version(workflow_id):
    """Build a minimal valid workflow version for publication tests."""
    return WorkflowVersion.draft(
        tenant_id=TENANT,
        workflow_definition_id=workflow_id,
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="close", from_state="intake", to_state="closed", human_driven=True
            ),
        ),
    )


@pytest.mark.asyncio
async def test_create_workflow__new_key__persists_header_and_audit() -> None:
    """Definition creation returns a draft-ready header and one audit event."""
    workflows = InMemoryWorkflowDefinitionRepository()
    audit = InMemoryAuditStore()
    workflow = await create_workflow.handle(
        create_workflow.CreateWorkflowCommand(
            tenant_id=TENANT,
            actor_id=ACTOR,
            key="ServiceRequestWorkflow",
            name="Service Request",
            description="Synthetic workflow.",
            correlation_id="corr-1",
        ),
        workflow_repo=workflows,
        audit_store=audit,
        clock=FixedClock(),
    )

    assert await workflows.get(TENANT, workflow.id) == workflow
    assert audit.events[0].event_type == "ConfigurationDrafted"


@pytest.mark.asyncio
async def test_publish_workflow__replay_same_published_version__returns_without_duplicate_audit() -> (  # noqa: E501 - convention name length
    None
):
    """Publishing the same workflow version twice is an idempotent replay."""
    workflow = make_workflow_definition(tenant_id=TENANT)
    version = _version(workflow.id)
    workflows = InMemoryWorkflowDefinitionRepository()
    await workflows.add(workflow)
    versions = InMemoryWorkflowVersionRepository()
    await versions.add(version)
    audit = InMemoryAuditStore()
    command = publish_workflow.PublishWorkflowVersionCommand(
        tenant_id=TENANT,
        actor_id=ACTOR,
        workflow_version_id=version.id,
        correlation_id="corr-2",
    )
    kwargs = {
        "workflow_repo": workflows,
        "workflow_version_repo": versions,
        "queue_repo": InMemoryQueueRepository(),
        "audit_store": audit,
        "clock": FixedClock(),
    }

    first = await publish_workflow.handle(command, **kwargs)
    second = await publish_workflow.handle(command, **kwargs)

    assert first == second
    assert len(audit.events) == 1
