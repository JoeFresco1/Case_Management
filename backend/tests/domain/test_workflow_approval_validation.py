"""Approval authoring validation (FM-017 / P02-US4).

Naming: test_<unit>_<scenario>__<expectation>, given/when/then.
"""

from __future__ import annotations

import pytest

from app.application.configuration.validate_workflow_definition import validate_definition
from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)


def _version_with(transitions: tuple[WorkflowTransition, ...]) -> WorkflowVersion:
    definition = WorkflowDefinition.create(
        tenant_id=__import__("uuid").UUID("11111111-1111-1111-1111-111111111111"),
        key="W",
        name="W",
    )
    states = (
        WorkflowState(key="intake", label="Intake", is_initial=True),
        WorkflowState(key="in_review", label="Review"),
        WorkflowState(key="closed", label="Closed", is_terminal=True),
    )
    return WorkflowVersion.draft(
        tenant_id=definition.tenant_id,
        workflow_definition_id=definition.id,
        version_number=1,
        states=states,
        transitions=transitions,
    )


def test_validate_approval__non_human_approval__rejected() -> None:
    # given — an approval transition that is automatic
    version = _version_with(
        (
            WorkflowTransition(
                key="decide",
                from_state="intake",
                to_state="in_review",
                human_driven=False,
                approval_required=True,
            ),
            WorkflowTransition(
                key="close", from_state="in_review", to_state="closed", human_driven=True
            ),
        )
    )
    # when / then — publish gate rejects
    with pytest.raises(ValidationFailed, match="human_driven"):
        validate_definition(version)


def test_validate_approval__empty_role_entry__rejected() -> None:
    # given — blank eligible role entry is rejected at the domain layer
    # (whitespace-only fails the isalnum check) so it can never reach publish

    with pytest.raises(InvariantViolation, match="eligible_approver_roles"):
        WorkflowTransition(
            key="decide",
            from_state="intake",
            to_state="in_review",
            human_driven=True,
            approval_required=True,
            eligible_approver_roles=("  ",),
        )


def test_validate_approval__valid_approval__accepted() -> None:
    # given — human-driven approval with eligible roles
    version = _version_with(
        (
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
        )
    )
    # when / then — no raise
    validate_definition(version)


def test_validate_approval__plain_workflow__accepted() -> None:
    # given — no approvals at all (additive-only gate)
    version = _version_with(
        (
            WorkflowTransition(
                key="decide",
                from_state="intake",
                to_state="in_review",
                human_driven=True,
            ),
            WorkflowTransition(
                key="close", from_state="in_review", to_state="closed", human_driven=True
            ),
        )
    )
    # when / then
    validate_definition(version)
