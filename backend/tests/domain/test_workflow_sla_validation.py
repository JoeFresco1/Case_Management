"""SLA authoring validation (FM-018).

Naming: test_<unit>_<scenario>__<expectation>, given/when/then.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.application.configuration.validate_workflow_definition import validate_definition
from app.domain.errors import ValidationFailed
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)

TENANT = UUID("11111111-1111-1111-1111-111111111111")


def _version_with(actions: tuple[dict, ...]) -> WorkflowVersion:
    definition = WorkflowDefinition.create(tenant_id=TENANT, key="W", name="W")
    return WorkflowVersion.draft(
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
                key="submit",
                from_state="intake",
                to_state="in_review",
                human_driven=True,
                actions=actions,
            ),
            WorkflowTransition(
                key="close", from_state="in_review", to_state="closed", human_driven=True
            ),
        ),
    )


def test_validate_sla__missing_sla_key__rejected() -> None:
    # given
    version = _version_with(({"type": "start_sla", "hours": 48, "queue": "q"},))
    # when / then
    with pytest.raises(ValidationFailed, match="sla_key"):
        validate_definition(version, known_queues=("q",))


def test_validate_sla__nonpositive_hours__rejected() -> None:
    # given
    version = _version_with(({"type": "start_sla", "sla_key": "k", "hours": 0, "queue": "q"},))
    # when / then
    with pytest.raises(ValidationFailed, match="hours"):
        validate_definition(version, known_queues=("q",))


def test_validate_sla__queue_less_escalation__rejected() -> None:
    # given — escalation create_task without a queue
    version = _version_with(
        (
            {
                "type": "start_sla",
                "sla_key": "k",
                "hours": 48,
                "queue": "q",
                "escalation_on_breach": {"type": "create_task"},
            },
        )
    )
    # when / then
    with pytest.raises(ValidationFailed, match="queue"):
        validate_definition(version, known_queues=("q",))


def test_validate_sla__unknown_escalation_queue__rejected() -> None:
    # given
    version = _version_with(
        (
            {
                "type": "start_sla",
                "sla_key": "k",
                "hours": 48,
                "queue": "q",
                "escalation_on_breach": {"type": "create_task", "queue": "nope"},
            },
        )
    )
    # when / then
    with pytest.raises(ValidationFailed, match="unknown queue"):
        validate_definition(version, known_queues=("q",))


def test_validate_sla__valid_sla_with_escalation__accepted() -> None:
    # given
    version = _version_with(
        (
            {
                "type": "start_sla",
                "sla_key": "review_due",
                "hours": 48,
                "queue": "q",
                "escalation_on_breach": {
                    "type": "create_task",
                    "queue": "q",
                    "task_type": "escalation",
                },
            },
        )
    )
    # when / then — no raise
    validate_definition(version, known_queues=("q",))


def test_validate_sla__notify_escalation__accepted() -> None:
    # given — notify is the deferred placeholder, accepted but scanner no-ops
    version = _version_with(
        (
            {
                "type": "start_sla",
                "sla_key": "k",
                "hours": 48,
                "queue": "q",
                "escalation_on_breach": {"type": "notify", "queue": "q"},
            },
        )
    )
    # when / then
    validate_definition(version, known_queues=("q",))
