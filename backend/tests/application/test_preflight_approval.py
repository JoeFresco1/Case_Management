"""Preflight approval surfacing (FM-017).

Naming: test_<unit>_<scenario>__<expectation>, given/when/then.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.application import authorization as authz_mod
from app.application.authorization import Policy, configure_default_policy
from app.application.workflow import preflight_transitions as preflight_uc
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)
from tests.application.workflow._execute_transition_fakes import (
    InMemoryCaseRepository,
    InMemoryTaskRepository,
    InMemoryWorkflowInstanceRepository,
    InMemoryWorkflowVersionRepository,
    StoredInstance,
)


@pytest.fixture(autouse=True)
def _reset_default_policy():  # type: ignore[no-untyped-def]
    original = authz_mod._default_policy
    configure_default_policy(Policy())
    yield
    configure_default_policy(original)


TENANT = UUID("11111111-1111-1111-1111-111111111111")
ORIGINATOR = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
APPROVER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


async def _setup() -> tuple[
    InMemoryCaseRepository,
    InMemoryTaskRepository,
    InMemoryWorkflowInstanceRepository,
    InMemoryWorkflowVersionRepository,
    Case,
]:
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    instance_repo = InMemoryWorkflowInstanceRepository()
    version_repo = InMemoryWorkflowVersionRepository()
    definition = WorkflowDefinition.create(tenant_id=TENANT, key="W", name="W")
    version = WorkflowVersion.draft(
        tenant_id=TENANT,
        workflow_definition_id=definition.id,
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="in_review", label="Review"),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="decide",
                from_state="intake",
                to_state="in_review",
                human_driven=True,
                approval_required=True,
                eligible_approver_roles=("supervisor",),
            ),
            WorkflowTransition(
                key="close", from_state="in_review", to_state="closed", human_driven=True
            ),
        ),
    ).publish(actor_id=uuid4(), at=NOW)
    version_repo._by_id[version.id] = version
    ctv = CaseTypeVersion(
        id=uuid4(),
        tenant_id=TENANT,
        case_type_id=uuid4(),
        version_number=1,
        schema=CaseTypeSchema(
            fields=(CaseTypeField(key="title", label="Title", type="text", required=True),)
        ),
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=NOW,
        published_by=ORIGINATOR,
    )
    case = Case.create(
        tenant_id=TENANT,
        reference=CaseReference(value="C-ACME-2026-000042"),
        case_type_version=ctv,
        title="t",
        description="",
        data={"title": "t"},
        priority="normal",
        created_by=ORIGINATOR,
        now=NOW,
        sensitivity="internal",
    )
    case_repo.add(case)
    instance_repo.add(
        StoredInstance(
            id=uuid4(),
            tenant_id=TENANT,
            case_id=case.id,
            workflow_definition_id=definition.id,
            workflow_version_id=version.id,
            current_state="intake",
            correlation_id="c",
            version_token=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return case_repo, task_repo, instance_repo, version_repo, case


async def test_preflight_approval__originator_sees_ineligible() -> None:
    # given
    case_repo, task_repo, instance_repo, version_repo, case = await _setup()
    # when
    result = await preflight_uc.handle(
        preflight_uc.PreflightTransitionsCommand(
            tenant_id=TENANT, actor_id=ORIGINATOR, actor_roles=("supervisor",), case_id=case.id
        ),
        case_repo=case_repo,
        instance_repo=instance_repo,
        workflow_version_repo=version_repo,
        task_repo=task_repo,
    )
    # then
    verdict = next(t for t in result.transitions if t.key == "decide")
    assert verdict.approval_required is True
    assert verdict.approval_eligible is False
    assert verdict.approval_reason is not None


async def test_preflight_approval__second_actor_sees_eligible() -> None:
    # given
    case_repo, task_repo, instance_repo, version_repo, case = await _setup()
    # when
    result = await preflight_uc.handle(
        preflight_uc.PreflightTransitionsCommand(
            tenant_id=TENANT, actor_id=APPROVER, actor_roles=("supervisor",), case_id=case.id
        ),
        case_repo=case_repo,
        instance_repo=instance_repo,
        workflow_version_repo=version_repo,
        task_repo=task_repo,
    )
    # then
    verdict = next(t for t in result.transitions if t.key == "decide")
    assert verdict.approval_required is True
    assert verdict.approval_eligible is True


async def test_preflight_approval__ineligible_role_sees_reason() -> None:
    # given
    case_repo, task_repo, instance_repo, version_repo, case = await _setup()
    # when
    result = await preflight_uc.handle(
        preflight_uc.PreflightTransitionsCommand(
            tenant_id=TENANT, actor_id=APPROVER, actor_roles=("reviewer",), case_id=case.id
        ),
        case_repo=case_repo,
        instance_repo=instance_repo,
        workflow_version_repo=version_repo,
        task_repo=task_repo,
    )
    # then
    verdict = next(t for t in result.transitions if t.key == "decide")
    assert verdict.approval_eligible is False
    assert "supervisor" in (verdict.approval_reason or "")
