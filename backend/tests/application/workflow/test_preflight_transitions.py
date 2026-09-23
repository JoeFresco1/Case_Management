"""Application tests for deterministic transition discovery (T080)."""

from dataclasses import replace

import pytest

from app.application import authorization as authz
from app.application.authorization import Policy, configure_default_policy
from app.application.workflow import preflight_transitions as preflight
from app.domain.errors import Forbidden, NotFound
from app.domain.workflow import WorkflowTransition
from tests.application.test_execute_transition import seeded as canonical_seeded  # noqa: F401


@pytest.fixture(autouse=True)
def _policy() -> None:
    """Use the static first-slice role registry for hermetic policy tests."""
    old = authz._default_policy
    configure_default_policy(Policy())
    yield
    configure_default_policy(old)


@pytest.mark.asyncio
async def test_preflight_transitions__allowed_and_blocked__returns_each_verdict(  # noqa: F811
    canonical_seeded,  # noqa: F811
):
    """The response contains available transitions without mutating state."""
    seeded = canonical_seeded
    version = seeded["version"]
    transition = WorkflowTransition(
        key="guarded",
        from_state="intake",
        to_state="in_review",
        guard={"eq": {"field": "case.priority", "value": "urgent"}},
    )
    seeded["workflow_version_repo"]._by_id[version.id] = replace(
        version, transitions=(version.transitions[0], transition)
    )
    result = await preflight.handle(
        preflight.PreflightTransitionsCommand(
            tenant_id=seeded["case"].tenant_id,
            actor_id=seeded["case"].created_by,
            actor_roles=("reviewer",),
            case_id=seeded["case"].id,
        ),
        case_repo=seeded["case_repo"],
        instance_repo=seeded["instance_repo"],
        workflow_version_repo=seeded["workflow_version_repo"],
        task_repo=seeded["task_repo"],
    )
    assert [item.key for item in result.transitions] == ["submit_for_review", "guarded"]
    assert result.transitions[0].allowed is True
    assert result.transitions[1].allowed is False
    assert result.transitions[1].reason == "rule_unmet"


@pytest.mark.asyncio
async def test_preflight_transitions__cross_tenant_case__returns_not_found(canonical_seeded):  # noqa: F811
    """Tenant-scoped case lookup hides a case belonging to another tenant."""
    seeded = canonical_seeded
    with pytest.raises(NotFound):
        await preflight.handle(
            preflight.PreflightTransitionsCommand(
                tenant_id=seeded["case"].tenant_id.__class__(
                    "22222222-2222-2222-2222-222222222222"
                ),
                actor_id=seeded["case"].created_by,
                actor_roles=("reviewer",),
                case_id=seeded["case"].id,
            ),
            case_repo=seeded["case_repo"],
            instance_repo=seeded["instance_repo"],
            workflow_version_repo=seeded["workflow_version_repo"],
            task_repo=seeded["task_repo"],
        )


@pytest.mark.asyncio
async def test_preflight_transitions__actor_without_transition_grant__is_forbidden(  # noqa: F811
    canonical_seeded,  # noqa: F811
):
    """A case reader cannot enumerate actionable workflow options."""
    seeded = canonical_seeded
    with pytest.raises(Forbidden):
        await preflight.handle(
            preflight.PreflightTransitionsCommand(
                tenant_id=seeded["case"].tenant_id,
                actor_id=seeded["case"].created_by,
                actor_roles=("intake",),
                case_id=seeded["case"].id,
            ),
            case_repo=seeded["case_repo"],
            instance_repo=seeded["instance_repo"],
            workflow_version_repo=seeded["workflow_version_repo"],
            task_repo=seeded["task_repo"],
        )
