"""Synthetic ServiceRequest workflow blueprint (P03-US5, FM-024).

Builds the in-memory ``intake → in_review → decided → closed`` workflow
definition the seed validates at seed time, so a malformed guard fails
the seed rather than passing through to a workflow that traps a case at
runtime.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.domain.workflow import WorkflowState, WorkflowTransition

__all__ = ["_service_request_workflow_blueprint"]


def _service_request_workflow_blueprint(
    *,
    tenant_id: UUID,  # noqa: ARG001 — reserved for future per-tenant guards
    known_classifications: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the synthetic ServiceRequest workflow blueprint (P03-US5).

    The blueprint mirrors the demo vertical: ``intake → in_review →
    decided → closed``. The ``submit_for_review`` transition is
    guarded by a document-requirements rule: at least one
    ``Contract``-classified document must be present on the case. A
    case that has not yet been assigned a contract cannot advance to
    review, and the run-time evaluator surfaces a ``missing_documents``
    failure with the missing classification in the details payload.

    The shape is intentionally close to what the test-suite fixtures
    build so the seed-time validation and the unit tests share the
    same contract. ``known_classifications`` is unused at the moment —
    the validator's cross-check is satisfied by the guard's
    ``classification`` field — but accepting the parameter makes the
    dependency explicit and lets the seed pass a per-tenant allowlist
    once the case type is multi-tenant configurable.
    """
    _ = tenant_id, known_classifications
    states: tuple[WorkflowState, ...] = (
        WorkflowState(key="intake", label="Intake", is_initial=True),
        WorkflowState(key="in_review", label="In review"),
        WorkflowState(key="decided", label="Decided"),
        WorkflowState(key="closed", label="Closed", is_terminal=True),
    )
    submit_for_review_guard: dict[str, Any] = {
        "op": "exists",
        "field": "case.documents_by_classification.Contract",
        "min": 1,
        "classification": "Contract",
    }
    transitions: tuple[WorkflowTransition, ...] = (
        WorkflowTransition(
            key="submit_for_review",
            from_state="intake",
            to_state="in_review",
            human_driven=True,
            guard=submit_for_review_guard,
            # Welcome actions (T070/T077): `start_workflow` materializes
            # these for every new case (initial "Review case" task + 48h
            # review SLA), and `execute_transition` fires them again when
            # the transition itself runs. Both actions carry an explicit
            # queue reference (publish validator requirement).
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
    )
    return {"states": states, "transitions": transitions}
