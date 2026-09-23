"""Validator tests for the workflow definition (P02-US2 / FM-013, plan §5).

Naming follows AGENTS.md §4.R5 (``test_<unit>_<scenario>__<expectation>``).
The acceptance scenarios for this slice are:

1. **Reachability**: every declared state is reachable from the initial
   state via some chain of transitions.
2. **Single initial state**: exactly one state has ``is_initial=True``.
3. **No ambiguous transitions**: duplicate transition keys are rejected.
4. **Unknown state references**: transitions may not point at states
   that do not exist on the same workflow.
5. **Terminal reachability**: every non-terminal state must be able to
   reach at least one terminal state. A state with no possible exit is
   rejected.
6. **Missing terminal**: a workflow with no terminal state at all is
   rejected.
7. **Invalid guard operator**: the validator rejects guards with an
   operator outside the closed declarative rule operator set.
8. **Unknown queue**: an action that references a queue not in the
   tenant's configured set is rejected.
9. **Automatic cycle rejected**: a non-terminating cycle composed
   entirely of ``human_driven=False`` transitions is rejected.
10. **Human-driven cycle allowed**: a cycle that contains at least
    one ``human_driven=True`` transition is accepted (the canonical
    ``intake -> in_review -> needs_info -> in_review`` scenario).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.application.configuration.validate_definition import (
    validate_definition,
)
from app.domain.errors import ValidationFailed
from app.domain.workflow import (
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Helpers — small, healthy building blocks
# ---------------------------------------------------------------------------


def _initial(key: str = "intake", label: str = "Intake") -> WorkflowState:
    return WorkflowState(key=key, label=label, is_initial=True)


def _terminal(key: str = "closed", label: str = "Closed") -> WorkflowState:
    return WorkflowState(key=key, label=label, is_terminal=True)


def _happy_path_version() -> WorkflowVersion:
    """A canonical 3-state workflow that passes every validator check.

    ``intake -> in_review -> closed`` with two human-driven transitions.
    """
    return WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(
            _initial(),
            WorkflowState(key="in_review", label="In Review"),
            _terminal(),
        ),
        transitions=(
            WorkflowTransition(
                key="submit_for_review",
                from_state="intake",
                to_state="in_review",
                human_driven=True,
            ),
            WorkflowTransition(
                key="close",
                from_state="in_review",
                to_state="closed",
                human_driven=True,
            ),
        ),
        now=None,
    )


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_validate_definition__happy_path__passes() -> None:
    """The canonical ``intake -> in_review -> closed`` workflow passes."""
    validate_definition(_happy_path_version())


# ---------------------------------------------------------------------------
# 2. Reachability
# ---------------------------------------------------------------------------


def test_validate_definition__unreachable_state__raises() -> None:
    """A state that no transition reaches is rejected."""
    states = (
        _initial(),
        WorkflowState(key="in_review", label="In Review"),
        WorkflowState(key="orphan", label="Orphan"),  # never reached
        _terminal(),
    )
    transitions = (
        WorkflowTransition(
            key="submit",
            from_state="intake",
            to_state="in_review",
            human_driven=True,
        ),
        WorkflowTransition(
            key="close",
            from_state="in_review",
            to_state="closed",
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=states,
        transitions=transitions,
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    msg = str(excinfo.value).lower()
    assert "unreachable" in msg or "terminal" in msg or "orphan" in msg


# ---------------------------------------------------------------------------
# 3. Initial state constraints
# ---------------------------------------------------------------------------


def test_validate_definition__no_initial_state__raises() -> None:
    """A workflow with no initial state is rejected."""
    states = (
        WorkflowState(key="in_review", label="In Review"),
        _terminal(),
    )
    transitions = (
        WorkflowTransition(
            key="close",
            from_state="in_review",
            to_state="closed",
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=states,
        transitions=transitions,
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    assert "initial" in str(excinfo.value).lower()


def test_validate_definition__two_initial_states__raises() -> None:
    """A workflow with two initial states is rejected."""
    states = (
        _initial(),
        WorkflowState(key="intake_b", label="Intake B", is_initial=True),
        _terminal(),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=states,
        transitions=(),
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    assert "initial" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# 4. Ambiguous transitions
# ---------------------------------------------------------------------------


def test_validate_definition__duplicate_transition_key__raises() -> None:
    """Two transitions sharing the same ``key`` are rejected (the
    v1 state machine is unambiguous)."""
    transitions = (
        WorkflowTransition(
            key="submit",
            from_state="intake",
            to_state="in_review",
            human_driven=True,
        ),
        WorkflowTransition(
            key="submit",  # duplicate key
            from_state="intake",
            to_state="closed",
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(
            _initial(),
            WorkflowState(key="in_review", label="In Review"),
            _terminal(),
        ),
        transitions=transitions,
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    assert "duplicate" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# 5. Unknown state references
# ---------------------------------------------------------------------------


def test_validate_definition__transition_to_unknown_state__raises() -> None:
    """A transition whose ``to_state`` is not declared is rejected."""
    transitions = (
        WorkflowTransition(
            key="submit",
            from_state="intake",
            to_state="ghost",  # not declared
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(_initial(), _terminal()),
        transitions=transitions,
        now=None,
    )

    with pytest.raises(ValidationFailed):
        validate_definition(version)


# ---------------------------------------------------------------------------
# 6. Missing terminal
# ---------------------------------------------------------------------------


def test_validate_definition__no_terminal_state__raises() -> None:
    """A workflow with no terminal state is rejected — the case must
    have a way to finish."""
    states = (
        _initial(),
        WorkflowState(key="in_review", label="In Review"),
    )
    transitions = (
        WorkflowTransition(
            key="submit",
            from_state="intake",
            to_state="in_review",
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=states,
        transitions=transitions,
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    assert "terminal" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# 7. Non-terminal state cannot reach a terminal state
# ---------------------------------------------------------------------------


def test_validate_definition__trapped_state__raises() -> None:
    """A non-terminal state with no path to a terminal state is
    rejected (the case would never end)."""
    states = (
        _initial(),
        WorkflowState(key="stuck", label="Stuck"),
        _terminal(),
    )
    transitions = (
        WorkflowTransition(
            key="go_stuck",
            from_state="intake",
            to_state="stuck",
            human_driven=True,
        ),
        # ``stuck`` has no outgoing edge → trap.
        WorkflowTransition(
            key="close",
            from_state="intake",  # ignores ``stuck`` deliberately
            to_state="closed",
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=states,
        transitions=transitions,
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    assert "stuck" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 8. Invalid guard operator
# ---------------------------------------------------------------------------


def test_validate_definition__invalid_guard_operator__raises() -> None:
    """A guard with an unknown operator is rejected."""
    transitions = (
        WorkflowTransition(
            key="submit",
            from_state="intake",
            to_state="in_review",
            human_driven=True,
            guard={"op": "weirdop", "field": "title"},
        ),
        WorkflowTransition(
            key="close",
            from_state="in_review",
            to_state="closed",
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(
            _initial(),
            WorkflowState(key="in_review", label="In Review"),
            _terminal(),
        ),
        transitions=transitions,
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    assert "weirdop" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 9. Unknown queue reference
# ---------------------------------------------------------------------------


def test_validate_definition__action_references_unknown_queue__raises() -> None:
    """An action whose ``queue`` is not in the configured queue set
    is rejected. The validator accepts the queue set as a parameter so
    it stays pure (no I/O); callers pass the tenant's currently
    configured queues."""
    transitions = (
        WorkflowTransition(
            key="submit",
            from_state="intake",
            to_state="in_review",
            human_driven=True,
            actions=(
                {
                    "type": "create_task",
                    "task_type": "review",
                    "queue": "ghost_queue",
                },
            ),
        ),
        WorkflowTransition(
            key="close",
            from_state="in_review",
            to_state="closed",
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(
            _initial(),
            WorkflowState(key="in_review", label="In Review"),
            _terminal(),
        ),
        transitions=transitions,
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version, known_queues=["review_queue"])
    assert "ghost_queue" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 10. Automatic cycle rejected (the runaway-loop scenario)
# ---------------------------------------------------------------------------


def test_validate_definition__automatic_cycle__raises() -> None:
    """A cycle composed entirely of ``human_driven=False`` transitions
    is rejected as a runaway automatic loop. The validator uses SCC
    analysis on the automatic-only subgraph to detect the cycle."""
    states = (
        _initial(),
        WorkflowState(key="auto_a", label="Auto A"),
        WorkflowState(key="auto_b", label="Auto B"),
        _terminal(),
    )
    transitions = (
        # Both transitions of the cycle are automatic.
        WorkflowTransition(
            key="a_to_b",
            from_state="auto_a",
            to_state="auto_b",
            human_driven=False,
        ),
        WorkflowTransition(
            key="b_to_a",
            from_state="auto_b",
            to_state="auto_a",
            human_driven=False,
        ),
        # How the cycle is reached and escaped.
        WorkflowTransition(
            key="into_cycle",
            from_state="intake",
            to_state="auto_a",
            human_driven=True,
        ),
        WorkflowTransition(
            key="out_of_cycle",
            from_state="auto_b",
            to_state="closed",
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=states,
        transitions=transitions,
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    assert "cycle" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# 11. Human-driven cycle allowed (the reviewer / needs_info scenario)
# ---------------------------------------------------------------------------


def test_validate_definition__human_driven_cycle__passes() -> None:
    """The canonical reviewer cycle ``intake -> in_review -> needs_info ->
    in_review`` is accepted because at least one transition in the cycle
    is ``human_driven=True`` — a human (or external event) can break
    the loop. The validator's automatic-cycle rejection is intentionally
    bypassed for breakable cycles."""
    states = (
        _initial(),
        WorkflowState(key="in_review", label="In Review"),
        WorkflowState(key="needs_info", label="Needs Info"),
        _terminal(),
    )
    transitions = (
        # Human-driven: intake → in_review
        WorkflowTransition(
            key="submit",
            from_state="intake",
            to_state="in_review",
            human_driven=True,
        ),
        # Human-driven: in_review → needs_info  (the breakable edge)
        WorkflowTransition(
            key="request_info",
            from_state="in_review",
            to_state="needs_info",
            human_driven=True,
        ),
        # Human-driven: needs_info → in_review  (closes the cycle)
        WorkflowTransition(
            key="re_review",
            from_state="needs_info",
            to_state="in_review",
            human_driven=True,
        ),
        # Final exit to terminal
        WorkflowTransition(
            key="close",
            from_state="in_review",
            to_state="closed",
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=states,
        transitions=transitions,
        now=None,
    )

    # Must not raise — the human-driven flag breaks the cycle.
    validate_definition(version)


# ---------------------------------------------------------------------------
# 12. Canonical single-key guards validate like authoring shape (T067)
# ---------------------------------------------------------------------------


def test_validate_definition__canonical_guard_shape__passes() -> None:
    """A canonical ``{"eq": {...}}`` guard validates exactly like its
    authoring ``{"op": "eq", ...}`` spelling (T067/T071 unification)."""
    transitions = (
        WorkflowTransition(
            key="submit",
            from_state="intake",
            to_state="in_review",
            human_driven=True,
            guard={"eq": {"field": "case.priority", "value": "high"}},
        ),
        WorkflowTransition(
            key="close",
            from_state="in_review",
            to_state="closed",
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(_initial(), WorkflowState(key="in_review", label="In Review"), _terminal()),
        transitions=transitions,
        now=None,
    )

    # Must not raise — same language the runtime evaluator speaks.
    validate_definition(version)


def test_validate_definition__canonical_guard_unknown_operator__fails() -> None:
    """An unknown operator fails in canonical shape too."""
    transitions = (
        WorkflowTransition(
            key="submit",
            from_state="intake",
            to_state="closed",
            human_driven=True,
            guard={"frobnicate": {"field": "case.priority"}},
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(_initial(), _terminal()),
        transitions=transitions,
        now=None,
    )

    with pytest.raises(ValidationFailed):
        validate_definition(version)


# ---------------------------------------------------------------------------
# 13. Forward reachability from the initial state (T072)
# ---------------------------------------------------------------------------


def test_validate_definition__unreachable_state__fails() -> None:
    """A state with no forward path from the initial state is rejected.

    ``limbo`` can reach the terminal (backward reachability holds) but
    no case can ever enter it - dead authoring the engine would carry
    forever.
    """
    transitions = (
        WorkflowTransition(
            key="submit",
            from_state="intake",
            to_state="in_review",
            human_driven=True,
        ),
        WorkflowTransition(
            key="close",
            from_state="in_review",
            to_state="closed",
            human_driven=True,
        ),
        WorkflowTransition(
            key="limbo_exit",
            from_state="limbo",
            to_state="closed",
            human_driven=True,
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(
            _initial(),
            WorkflowState(key="in_review", label="In Review"),
            WorkflowState(key="limbo", label="Limbo"),
            _terminal(),
        ),
        transitions=transitions,
        now=None,
    )

    with pytest.raises(ValidationFailed, match="unreachable from the initial"):
        validate_definition(version)
