"""Structural validation: states, transitions, approvals, composition.

This module owns the declaration-level checks over the workflow JSON —
the pieces that do not need a graph walk (those live in
:mod:`graph`) and do not parse guards/actions (those live in
:mod:`guards` / :mod:`actions`):

- :func:`_validate_states` — non-empty, unique keys, exactly one
  ``is_initial`` state.
- :func:`_validate_transitions` — unique keys, known ``from``/``to``,
  no edge out of a terminal state or into the initial state, guard
  shape (via :mod:`guards`), and subprocess references.
- :func:`_validate_approvals` — FM-017/P02-US4 separation-of-duties
  authoring rules (approval transitions must be human-driven; approver
  role entries must be non-empty strings).
- :func:`_validate_subprocesses` — FM-019/P02-US7 reusable fragment
  machines: unique keys, initial + terminal states, known edges, and
  no collision with top-level state keys.
- :func:`_validate_parallel` — FM-019/P02-US8 branch/join semantics:
  declared branch endpoints, join reachability, ``count``-mode bounds,
  and mutual exclusion with ``subprocess_key``.

Pure functions: no I/O. Split from the pre-R2 single-file validator
(``validate_workflow_definition.py``) by responsibility.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.domain.errors import ValidationFailed
from app.domain.workflow import (
    JoinMode,
    WorkflowState,
    WorkflowVersion,
)

from .graph import _can_reach
from .guards import _validate_guard_shape


def _validate_states(
    states: tuple[WorkflowState, ...],
) -> dict[str, WorkflowState]:
    """Index declared states and enforce the initial-state invariant.

    Args:
        states: the version's declared states in authoring order.

    Returns:
        The states indexed by ``key`` for downstream checks.

    Raises:
        ValidationFailed: empty state list, duplicate key, or not
            exactly one ``is_initial`` state.
    """
    if not states:
        raise ValidationFailed(
            "workflow definition requires at least one state",
            field="states",
        )

    seen: set[str] = set()
    initial_count = 0
    by_key: dict[str, WorkflowState] = {}
    for state in states:
        if state.key in seen:
            raise ValidationFailed(
                f"duplicate state key: {state.key!r}", field=f"states.{state.key}"
            )
        seen.add(state.key)
        by_key[state.key] = state
        if state.is_initial:
            initial_count += 1

    if initial_count == 0:
        raise ValidationFailed(
            "workflow definition requires exactly one initial state",
            field="states",
        )
    if initial_count > 1:
        raise ValidationFailed(
            f"workflow definition has {initial_count} initial states; expected exactly 1",
            field="states",
        )
    return by_key


def _validate_transitions(
    version: WorkflowVersion,
    states_by_key: dict[str, WorkflowState],
    known_classifications: Iterable[str] = (),
) -> None:
    """Validate transition keys, endpoints, guards, and subprocess refs.

    Args:
        version: the draft workflow version under review.
        states_by_key: declared states indexed by key.
        known_classifications: document classification labels for the
            guard cross-check (empty skips the membership check).

    Raises:
        ValidationFailed: duplicate key, unknown endpoint, an edge out
            of a terminal state or into the initial state, a malformed
            guard, or a bad ``subprocess_key`` reference (including a
            non-human-driven subprocess invocation).
    """
    seen_keys: set[str] = set()
    for transition in version.transitions:
        if transition.key in seen_keys:
            raise ValidationFailed(
                f"duplicate transition key: {transition.key!r}",
                field=f"transitions.{transition.key}",
            )
        seen_keys.add(transition.key)

        if transition.from_state not in states_by_key:
            raise ValidationFailed(
                f"transition {transition.key!r} references unknown from_state "
                f"{transition.from_state!r}",
                field=f"transitions.{transition.key}.from",
            )
        if transition.to_state not in states_by_key:
            raise ValidationFailed(
                f"transition {transition.key!r} references unknown to_state "
                f"{transition.to_state!r}",
                field=f"transitions.{transition.key}.to",
            )

        from_state = states_by_key[transition.from_state]
        if from_state.is_terminal:
            raise ValidationFailed(
                f"transition {transition.key!r} originates from terminal state "
                f"{transition.from_state!r}",
                field=f"transitions.{transition.key}.from",
            )
        to_state = states_by_key[transition.to_state]
        if to_state.is_initial:
            raise ValidationFailed(
                f"transition {transition.key!r} targets initial state {transition.to_state!r}",
                field=f"transitions.{transition.key}.to",
            )

        if transition.guard is not None:
            _validate_guard_shape(
                transition.guard,
                path=f"transitions[{transition.key}].guard",
                depth=0,
                known_classifications=known_classifications,
            )

        if transition.subprocess_key is not None:
            if version.get_subprocess(transition.subprocess_key) is None:
                raise ValidationFailed(
                    f"transition {transition.key!r} references unknown "
                    f"subprocess {transition.subprocess_key!r}",
                    field=f"transitions[{transition.key}].subprocess_key",
                )
            # Subprocess invocations must be human-driven: a subprocess
            # that fires automatically without a breakable point could
            # trap a case inside the fragment (no human, no event).
            if not transition.human_driven:
                raise ValidationFailed(
                    f"transition {transition.key!r} invokes a subprocess and must be human_driven",
                    field=f"transitions[{transition.key}]",
                )


def _validate_approvals(version: WorkflowVersion) -> None:
    """Reject incoherent approval configuration at publish time.

    Rules (additive-only; existing definitions without approvals pass):

    - an ``approval_required`` transition must be ``human_driven`` — a
      consequential decision always needs a human approver; an
      automatic approval would be a runaway self-approval.
    - every entry in ``eligible_approver_roles`` must be a non-empty
      string (whitespace-only rejected). The role registry is
      stringly-checked here: roles are tenant-configured at runtime
      (FM-048), so the validator cannot authoritatively resolve them
      without I/O — the engine enforces the gate fail-closed at
      runtime instead. An empty tuple means "any role with
      ``case.transition`` who is not the originator".

    Args:
        version: the draft workflow version under review.

    Raises:
        ValidationFailed: an approval transition is automatic, or an
            approver-role entry is empty.
    """
    for transition in version.transitions:
        path = f"transitions[{transition.key}]"
        if transition.approval_required and not transition.human_driven:
            raise ValidationFailed(
                f"transition {transition.key!r} requires approval and must be human_driven",
                field=f"{path}.human_driven",
            )
        for role in transition.eligible_approver_roles:
            if not isinstance(role, str) or not role.strip():
                raise ValidationFailed(
                    f"transition {transition.key!r} has an empty eligible_approver_roles entry",
                    field=f"{path}.eligible_approver_roles",
                )


def _validate_subprocesses(
    version: WorkflowVersion,
    states_by_key: dict[str, WorkflowState],
) -> None:
    """Validate reusable subprocess fragments declared on the version.

    Each subprocess is itself a state machine: it must declare at
    least one state, its state and transition keys must be unique,
    and its states must include at least one initial state and at
    least one terminal state (otherwise the fragment cannot be
    entered or exited — it would trap the case inside the subprocess).
    The transition's ``subprocess_key`` reference is enforced in
    :func:`_validate_transitions`.

    Args:
        version: the draft workflow version under review.
        states_by_key: top-level states indexed by key (for namespace
            collision checks).

    Raises:
        ValidationFailed: duplicate subprocess key, missing initial or
            terminal state, unknown edge endpoint, or a subprocess
            state key colliding with a top-level state key.
    """
    seen: set[str] = set()
    for sub in version.subprocesses:
        if sub.key in seen:
            raise ValidationFailed(
                f"duplicate subprocess key: {sub.key!r}",
                field=f"subprocesses.{sub.key}",
            )
        seen.add(sub.key)

        sub_state_keys = [s.key for s in sub.states]
        sub_by_key = dict(zip(sub_state_keys, sub.states, strict=True))
        if not any(s.is_initial for s in sub.states):
            raise ValidationFailed(
                f"subprocess {sub.key!r} requires an initial state",
                field=f"subprocesses.{sub.key}.states",
            )
        if not any(s.is_terminal for s in sub.states):
            raise ValidationFailed(
                f"subprocess {sub.key!r} requires at least one terminal state",
                field=f"subprocesses.{sub.key}.states",
            )

        for transition in sub.transitions:
            if transition.from_state not in sub_by_key:
                raise ValidationFailed(
                    f"subprocess {sub.key!r} transition "
                    f"{transition.key!r} references unknown from_state",
                    field=f"subprocesses.{sub.key}.transitions",
                )
            if transition.to_state not in sub_by_key:
                raise ValidationFailed(
                    f"subprocess {sub.key!r} transition "
                    f"{transition.key!r} references unknown to_state",
                    field=f"subprocesses.{sub.key}.transitions",
                )

        # A subprocess must not collide with a top-level state key —
        # the runtime treats state keys as a single namespace per
        # workflow instance and a collision would be ambiguous.
        for state in sub.states:
            if state.key in states_by_key:
                raise ValidationFailed(
                    f"subprocess {sub.key!r} state {state.key!r} collides "
                    "with a top-level workflow state",
                    field=f"subprocesses.{sub.key}.states.{state.key}",
                )


def _validate_parallel(
    version: WorkflowVersion,
    states_by_key: dict[str, WorkflowState],
) -> None:
    """Validate parallel branches and join semantics.

    For every transition that declares ``branches`` we confirm:

    - the join target is a declared state and is reachable from
      every branch terminal state (otherwise ``mode='all'`` would
      never fire);
    - each branch's ``from_state`` and ``terminal_state`` are declared
      on the version;
    - a ``mode='count'`` join's ``min_completions`` lies in
      ``[1, len(branches)]``;
    - branch keys are unique (already enforced by the dataclass but
      repeated here with a precise field path).

    Args:
        version: the draft workflow version under review.
        states_by_key: declared states indexed by key.

    Raises:
        ValidationFailed: a branch endpoint or join target is not a
            declared state, a branch cannot reach the join, a count
            join is out of bounds, or the transition also references a
            subprocess.
    """
    adjacency: dict[str, set[str]] = {key: set() for key in states_by_key}
    for transition in version.transitions:
        adjacency[transition.from_state].add(transition.to_state)

    for transition in version.parallel_transitions():
        join = transition.join
        assert join is not None  # parallel_transitions invariant
        path = f"transitions[{transition.key}]"

        if join.target_state not in states_by_key:
            raise ValidationFailed(
                f"parallel transition {transition.key!r} join target "
                f"{join.target_state!r} is not a declared state",
                field=f"{path}.join.target_state",
            )

        for branch in transition.branches:
            branch_path = f"{path}.branches[{branch.branch_key}]"
            if branch.from_state not in states_by_key:
                raise ValidationFailed(
                    f"parallel branch {branch.branch_key!r} from_state "
                    f"{branch.from_state!r} is not a declared state",
                    field=f"{branch_path}.from_state",
                )
            if branch.terminal_state not in states_by_key:
                raise ValidationFailed(
                    f"parallel branch {branch.branch_key!r} terminal_state "
                    f"{branch.terminal_state!r} is not a declared state",
                    field=f"{branch_path}.terminal_state",
                )
            # Each branch terminal must be able to reach the join
            # target; otherwise mode='all' can never fire.
            if not _can_reach(branch.terminal_state, adjacency, join.target_state):
                raise ValidationFailed(
                    f"parallel branch {branch.branch_key!r} terminal_state "
                    f"{branch.terminal_state!r} cannot reach join target "
                    f"{join.target_state!r}",
                    field=branch_path,
                )

        if join.mode is JoinMode.COUNT:
            n = len(transition.branches)
            if join.min_completions is None or join.min_completions < 1:
                raise ValidationFailed(
                    f"parallel transition {transition.key!r} join mode "
                    "'count' requires min_completions >= 1",
                    field=f"{path}.join.min_completions",
                )
            if join.min_completions > n:
                raise ValidationFailed(
                    f"parallel transition {transition.key!r} join "
                    f"min_completions={join.min_completions} exceeds "
                    f"branch count {n}",
                    field=f"{path}.join.min_completions",
                )

        # Reference: subprocess_key and branches are mutually exclusive
        # (already enforced by the WorkflowTransition dataclass).
        if transition.subprocess_key is not None:
            raise ValidationFailed(
                f"parallel transition {transition.key!r} cannot also reference a subprocess",
                field=path,
            )
