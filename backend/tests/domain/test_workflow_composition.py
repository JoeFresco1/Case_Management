"""Domain + validator tests for workflow composition (FM-019).

Naming follows AGENTS.md §4.R5 (``test_<unit>_<scenario>__<expectation>``).
Acceptance for this slice (P02-US7 / P02-US8):

1. **Reusable subprocesses** — a transition may declare a
   ``subprocess_key``; the validator confirms the fragment exists, is
   well-formed (initial + terminal states, unique keys), and the
   invocation is human-driven.
2. **Parallel branches with explicit join** — a transition may
   declare ``branches`` plus a ``join``; the validator confirms each
   branch terminal can reach the join target, the join mode shape is
   consistent (``count`` requires ``min_completions`` in
   ``[1, len(branches)]``), and the top-level state machine is still
   reachable from the initial state.
3. **Join execution semantics** — :func:`evaluate_join` fires only
   after the appropriate number of branches complete, depending on
   mode (``all`` / ``any`` / ``count``).

The composition constructs extend the existing v1 state-machine
model without weakening the v1 invariants (single initial state,
unique transition keys, terminal-reachability). The validator's
happy-path composition is the canonical "parallel review" pattern:

    intake ──split──▶ in_review_a ──▶ reviewed_a ┐
                                                ├─ join → decided
                     in_review_b ──▶ reviewed_b ─┘
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.application.configuration.validate_definition import validate_definition
from app.application.workflow.execute_transition import evaluate_join
from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.workflow import (
    BranchSpec,
    JoinMode,
    JoinSpec,
    Subprocess,
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _initial(key: str = "intake") -> WorkflowState:
    return WorkflowState(key=key, label="Intake", is_initial=True)


def _terminal(key: str = "closed") -> WorkflowState:
    return WorkflowState(key=key, label="Closed", is_terminal=True)


def _parallel_review_version() -> WorkflowVersion:
    """A canonical two-branch parallel review that passes every check.

    intake → fan_out → {in_review_a, in_review_b} → join → decided
    """
    return WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(
            _initial(),
            WorkflowState(key="fan_out", label="Fan Out"),
            WorkflowState(key="in_review_a", label="Review A"),
            WorkflowState(key="in_review_b", label="Review B"),
            WorkflowState(key="reviewed_a", label="Reviewed A"),
            WorkflowState(key="reviewed_b", label="Reviewed B"),
            WorkflowState(key="decided", label="Decided"),
            _terminal(),
        ),
        transitions=(
            WorkflowTransition(
                key="start_parallel",
                from_state="intake",
                to_state="fan_out",
                human_driven=True,
            ),
            WorkflowTransition(
                key="fan_out",
                from_state="fan_out",
                to_state="in_review_a",
                human_driven=True,
                branches=(
                    BranchSpec(
                        branch_key="a",
                        from_state="in_review_a",
                        terminal_state="reviewed_a",
                    ),
                    BranchSpec(
                        branch_key="b",
                        from_state="in_review_b",
                        terminal_state="reviewed_b",
                    ),
                ),
                join=JoinSpec(target_state="decided", mode=JoinMode.ALL),
            ),
            # Each branch advances from its review state to its terminal
            # state, then transitions to ``decided`` so the join target
            # is reachable from every branch terminal.
            WorkflowTransition(
                key="finish_review_a",
                from_state="in_review_a",
                to_state="reviewed_a",
                human_driven=True,
            ),
            WorkflowTransition(
                key="finish_review_b",
                from_state="in_review_b",
                to_state="reviewed_b",
                human_driven=True,
            ),
            WorkflowTransition(
                key="review_a_done",
                from_state="reviewed_a",
                to_state="decided",
                human_driven=True,
            ),
            WorkflowTransition(
                key="review_b_done",
                from_state="reviewed_b",
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
        now=None,
    )


# ---------------------------------------------------------------------------
# 1. Subprocess: declaration, validation, and reference
# ---------------------------------------------------------------------------


def test_subprocess__duplicate_state_key__raises_invariant() -> None:
    """A subprocess fragment with duplicate state keys is rejected at
    construction time (the dataclass is the first guard)."""
    with pytest.raises(InvariantViolation):
        Subprocess(
            key="notify",
            states=(
                WorkflowState(key="start", label="Start", is_initial=True),
                WorkflowState(key="start", label="Dup", is_terminal=True),
            ),
            transitions=(),
        )


def test_subprocess__no_initial_state__rejected_by_validator() -> None:
    """A subprocess without an initial state is rejected by the
    validator (the fragment cannot be entered)."""
    states = (
        _initial(),
        _terminal(),
    )
    transitions = (
        WorkflowTransition(
            key="invoke_notify",
            from_state="intake",
            to_state="closed",
            human_driven=True,
            subprocess_key="notify",
        ),
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=states,
        transitions=transitions,
        subprocesses=(
            Subprocess(
                key="notify",
                states=(WorkflowState(key="only", label="Only"),),
                transitions=(),
            ),
        ),
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    assert "initial" in str(excinfo.value).lower()


def test_subprocess__unknown_reference__rejected_by_validator() -> None:
    """A transition that references an undeclared subprocess is
    rejected."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(_initial(), _terminal()),
        transitions=(
            WorkflowTransition(
                key="invoke_ghost",
                from_state="intake",
                to_state="closed",
                human_driven=True,
                subprocess_key="ghost",
            ),
        ),
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    assert "ghost" in str(excinfo.value)


def test_subprocess__happy_path__passes_validator() -> None:
    """A well-formed subprocess is accepted by the validator."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(_initial(), _terminal()),
        transitions=(
            WorkflowTransition(
                key="notify_then_close",
                from_state="intake",
                to_state="closed",
                human_driven=True,
                subprocess_key="notify",
            ),
        ),
        subprocesses=(
            Subprocess(
                key="notify",
                states=(
                    WorkflowState(key="compose", label="Compose", is_initial=True),
                    WorkflowState(key="sent", label="Sent", is_terminal=True),
                ),
                transitions=(
                    WorkflowTransition(
                        key="send",
                        from_state="compose",
                        to_state="sent",
                        human_driven=True,
                    ),
                ),
            ),
        ),
        now=None,
    )

    validate_definition(version)  # must not raise


def test_subprocess__automatic_invocation__rejected() -> None:
    """A subprocess invoked by an automatic (non-human-driven)
    transition is rejected — the fragment would trap the case
    because nothing inside it can be broken without a human."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(_initial(), _terminal()),
        transitions=(
            WorkflowTransition(
                key="auto_notify",
                from_state="intake",
                to_state="closed",
                human_driven=False,  # automatic
                subprocess_key="notify",
            ),
        ),
        subprocesses=(
            Subprocess(
                key="notify",
                states=(
                    WorkflowState(key="compose", label="Compose", is_initial=True),
                    WorkflowState(key="sent", label="Sent", is_terminal=True),
                ),
                transitions=(),
            ),
        ),
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    assert "human_driven" in str(excinfo.value)


def test_subprocess_key_and_branches_mutually_exclusive__raises_invariant() -> None:
    """A transition cannot declare both a subprocess invocation and a
    parallel section — composition is one or the other."""
    with pytest.raises(InvariantViolation):
        WorkflowTransition(
            key="bad",
            from_state="intake",
            to_state="closed",
            human_driven=True,
            subprocess_key="notify",
            branches=(BranchSpec("a", "in_review_a", "reviewed_a"),),
            join=JoinSpec(target_state="closed", mode=JoinMode.ALL),
        )


# ---------------------------------------------------------------------------
# 2. Parallel branches: validator
# ---------------------------------------------------------------------------


def test_parallel__happy_path__passes_validator() -> None:
    """The canonical parallel-review workflow validates cleanly."""
    validate_definition(_parallel_review_version())


def test_parallel__branch_terminal_cannot_reach_join__rejected() -> None:
    """A branch whose terminal state cannot reach the join target is
    rejected — ``mode='all'`` would never fire in that case."""
    states = (
        _initial(),
        WorkflowState(key="fan_out", label="Fan Out"),
        WorkflowState(key="in_review_a", label="A"),
        WorkflowState(key="in_review_b", label="B"),
        WorkflowState(key="reviewed_a", label="A done"),
        WorkflowState(key="decided", label="Decided"),
        _terminal(),
        # Branch B's terminal is reachable from the workflow as a whole
        # (it can still transition to ``closed``), but the join target
        # is ``decided`` - so this branch can never satisfy the join.
        WorkflowState(key="diverged", label="Diverged"),
    )
    transitions = (
        WorkflowTransition(
            key="start_parallel",
            from_state="intake",
            to_state="fan_out",
            human_driven=True,
        ),
        WorkflowTransition(
            key="fan_out",
            from_state="fan_out",
            to_state="in_review_a",
            human_driven=True,
            branches=(
                BranchSpec("a", "in_review_a", "reviewed_a"),
                BranchSpec("b", "in_review_b", "diverged"),
            ),
            join=JoinSpec(target_state="decided", mode=JoinMode.ALL),
        ),
        WorkflowTransition(
            key="finish_a",
            from_state="in_review_a",
            to_state="reviewed_a",
            human_driven=True,
        ),
        WorkflowTransition(
            key="finish_b",
            from_state="in_review_b",
            to_state="diverged",
            human_driven=True,
        ),
        WorkflowTransition(
            key="review_a_done",
            from_state="reviewed_a",
            to_state="decided",
            human_driven=True,
        ),
        # Branch B diverges to ``closed`` instead of ``decided`` so the
        # join target is unreachable from its terminal.
        WorkflowTransition(
            key="diverged_close",
            from_state="diverged",
            to_state="closed",
            human_driven=True,
        ),
        WorkflowTransition(
            key="close",
            from_state="decided",
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
    assert "diverged" in str(excinfo.value).lower()


def test_parallel__count_min_completions_too_large__rejected() -> None:
    """A ``count`` join whose ``min_completions`` exceeds the branch
    count is rejected."""
    states = (
        _initial(),
        WorkflowState(key="fan_out", label="Fan Out"),
        WorkflowState(key="in_review_a", label="A"),
        WorkflowState(key="in_review_b", label="B"),
        WorkflowState(key="reviewed_a", label="A done"),
        WorkflowState(key="reviewed_b", label="B done"),
        WorkflowState(key="decided", label="Decided"),
        _terminal(),
    )
    transitions = (
        WorkflowTransition(
            key="start_parallel",
            from_state="intake",
            to_state="fan_out",
            human_driven=True,
        ),
        WorkflowTransition(
            key="fan_out",
            from_state="fan_out",
            to_state="in_review_a",
            human_driven=True,
            branches=(
                BranchSpec("a", "in_review_a", "reviewed_a"),
                BranchSpec("b", "in_review_b", "reviewed_b"),
            ),
            join=JoinSpec(
                target_state="decided",
                mode=JoinMode.COUNT,
                min_completions=5,  # only 2 branches
            ),
        ),
        WorkflowTransition(
            key="finish_a",
            from_state="in_review_a",
            to_state="reviewed_a",
            human_driven=True,
        ),
        WorkflowTransition(
            key="finish_b",
            from_state="in_review_b",
            to_state="reviewed_b",
            human_driven=True,
        ),
        WorkflowTransition(
            key="a_done",
            from_state="reviewed_a",
            to_state="decided",
            human_driven=True,
        ),
        WorkflowTransition(
            key="b_done",
            from_state="reviewed_b",
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
    assert "min_completions" in str(excinfo.value)


def test_parallel__branch_states_must_be_declared__rejected() -> None:
    """A branch whose ``from_state`` is not declared on the version
    is rejected."""
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=uuid4(),
        version_number=1,
        states=(
            _initial(),
            WorkflowState(key="fan_out", label="Fan Out"),
            WorkflowState(key="in_review_a", label="A"),
            WorkflowState(key="reviewed_a", label="A done"),
            WorkflowState(key="decided", label="Decided"),
            _terminal(),
        ),
        transitions=(
            WorkflowTransition(
                key="start_parallel",
                from_state="intake",
                to_state="fan_out",
                human_driven=True,
            ),
            WorkflowTransition(
                key="fan_out",
                from_state="fan_out",
                to_state="in_review_a",
                human_driven=True,
                branches=(
                    BranchSpec("a", "in_review_a", "reviewed_a"),
                    BranchSpec("b", "ghost", "reviewed_a"),
                ),
                join=JoinSpec(target_state="decided", mode=JoinMode.ALL),
            ),
            WorkflowTransition(
                key="finish_a",
                from_state="in_review_a",
                to_state="reviewed_a",
                human_driven=True,
            ),
            WorkflowTransition(
                key="a_done",
                from_state="reviewed_a",
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
        now=None,
    )

    with pytest.raises(ValidationFailed) as excinfo:
        validate_definition(version)
    assert "ghost" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. Parallel branches: join execution semantics
# ---------------------------------------------------------------------------


def _parallel_review_transition() -> WorkflowTransition:
    return WorkflowTransition(
        key="fan_out",
        from_state="fan_out",
        to_state="in_review_a",
        human_driven=True,
        branches=(
            BranchSpec("a", "in_review_a", "reviewed_a"),
            BranchSpec("b", "in_review_b", "reviewed_b"),
        ),
        join=JoinSpec(target_state="decided", mode=JoinMode.ALL),
    )


def test_join__all_mode_partial_completion__does_not_fire() -> None:
    """With ``mode='all'`` the join must wait for every declared branch
    to reach its terminal — partial completion leaves the case in the
    parallel span."""
    transition = _parallel_review_transition()
    decision = evaluate_join(transition, completed_branch_keys=("a",))

    assert decision.fire is False
    assert decision.next_state is None
    assert decision.completed_branches == ("a",)


def test_join__all_mode_full_completion__fires_and_targets_join_state() -> None:
    """When every branch has completed under ``mode='all'`` the join
    fires and the case advances to ``join.target_state``."""
    transition = _parallel_review_transition()
    decision = evaluate_join(transition, completed_branch_keys=("a", "b"))

    assert decision.fire is True
    assert decision.next_state == "decided"
    assert decision.completed_branches == ("a", "b")


def test_join__all_mode_completion_order_is_preserved() -> None:
    """``completed_branches`` is reported in the declared branch order,
    not the completion order, so the audit row is deterministic."""
    transition = _parallel_review_transition()
    decision = evaluate_join(transition, completed_branch_keys=("b", "a"))

    assert decision.fire is True
    assert decision.completed_branches == ("a", "b")


def test_join__duplicate_completion_keys__are_deduplicated_in_declared_order() -> None:
    """Retries cannot change the deterministic set of completed branches."""
    transition = _parallel_review_transition()

    decision = evaluate_join(transition, completed_branch_keys=("b", "a", "a"))

    assert decision.fire is True
    assert decision.completed_branches == ("a", "b")


def test_join__any_mode_first_completion__fires_immediately() -> None:
    """With ``mode='any'`` the join fires as soon as one branch
    completes — the remaining branches are abandoned."""
    transition = WorkflowTransition(
        key="any_join",
        from_state="fan_out",
        to_state="in_review_a",
        human_driven=True,
        branches=(
            BranchSpec("a", "in_review_a", "reviewed_a"),
            BranchSpec("b", "in_review_b", "reviewed_b"),
        ),
        join=JoinSpec(target_state="decided", mode=JoinMode.ANY),
    )

    decision = evaluate_join(transition, completed_branch_keys=("b",))
    assert decision.fire is True
    assert decision.next_state == "decided"
    assert decision.completed_branches == ("b",)


def test_join__any_mode_no_completion__does_not_fire() -> None:
    """With ``mode='any'`` and no completed branches the join is silent."""
    transition = WorkflowTransition(
        key="any_join",
        from_state="fan_out",
        to_state="in_review_a",
        human_driven=True,
        branches=(
            BranchSpec("a", "in_review_a", "reviewed_a"),
            BranchSpec("b", "in_review_b", "reviewed_b"),
        ),
        join=JoinSpec(target_state="decided", mode=JoinMode.ANY),
    )

    decision = evaluate_join(transition, completed_branch_keys=())
    assert decision.fire is False
    assert decision.next_state is None


def test_join__count_mode_threshold_met__fires() -> None:
    """With ``mode='count'`` the join fires once ``min_completions``
    branches have completed, even if others are still running."""
    transition = WorkflowTransition(
        key="count_join",
        from_state="fan_out",
        to_state="in_review_a",
        human_driven=True,
        branches=(
            BranchSpec("a", "in_review_a", "reviewed_a"),
            BranchSpec("b", "in_review_b", "reviewed_b"),
            BranchSpec("c", "in_review_c", "reviewed_c"),
        ),
        join=JoinSpec(
            target_state="decided",
            mode=JoinMode.COUNT,
            min_completions=2,
        ),
    )

    decision = evaluate_join(transition, completed_branch_keys=("a", "b"))
    assert decision.fire is True
    assert decision.next_state == "decided"
    assert decision.completed_branches == ("a", "b")


def test_join__count_mode_threshold_not_met__does_not_fire() -> None:
    """With ``mode='count'`` and fewer completions than the threshold,
    the join is silent."""
    transition = WorkflowTransition(
        key="count_join",
        from_state="fan_out",
        to_state="in_review_a",
        human_driven=True,
        branches=(
            BranchSpec("a", "in_review_a", "reviewed_a"),
            BranchSpec("b", "in_review_b", "reviewed_b"),
            BranchSpec("c", "in_review_c", "reviewed_c"),
        ),
        join=JoinSpec(
            target_state="decided",
            mode=JoinMode.COUNT,
            min_completions=2,
        ),
    )

    decision = evaluate_join(transition, completed_branch_keys=("a",))
    assert decision.fire is False


def test_join__unknown_branch_keys_ignored() -> None:
    """Branch keys that are not declared on the transition are ignored
    by the evaluator (defensive: the runtime only reports declared
    branches, but a stale value should not poison the join)."""
    transition = _parallel_review_transition()
    decision = evaluate_join(
        transition,
        completed_branch_keys=("a", "ghost"),
    )
    # ``ghost`` is filtered out; ``a`` alone is not enough under ALL.
    assert decision.fire is False
    assert decision.completed_branches == ("a",)


def test_evaluate_join__non_parallel_transition__raises_invariant() -> None:
    """``evaluate_join`` rejects a transition that is not a parallel
    section — the function is meaningful only for ``branches``."""
    with pytest.raises(InvariantViolation):
        evaluate_join(
            WorkflowTransition(
                key="plain",
                from_state="intake",
                to_state="closed",
                human_driven=True,
            ),
            completed_branch_keys=(),
        )
