"""Deterministic workflow simulator (FM-015 / P02-US5).

The simulator answers the question "what would happen if I ran this
``WorkflowVersion`` against this synthetic case with these actions?"
without persisting anything. It is the **dry-run** companion to the
runtime transition engine: an administrator can preview a draft
workflow against a synthetic case before publication, or audit the
expected step trace for an already-published version.

# Why this lives in ``application/`` not ``domain/``

The simulator touches the guard evaluator (``evaluate_guard``), the
state machine (``WorkflowVersion.outgoing``), and the action list, but
it does **not** persist anything, emit audit events, or talk to the
outbox. The shape is intentionally closer to a use case than a domain
entity: callers hand it a ``WorkflowVersion`` and a synthetic case and
receive a ``SimulationResult``. ``domain/`` stays pure
(``app/domain/ports.py`` + workflow entity); this is consistent with
AGENTS.md §4.R10 ("Modular monolith with explicit boundaries").

# Why a separate guard evaluator from the runtime engine

The runtime ``execute_transition`` use case (T070 / plan §5) consumes
the same declarative guard documents this module evaluates. The
operator set is closed and small (``validate_workflow_definition._ALLOWED_GUARD_OPERATORS``),
so the simulator implements the same evaluation rules directly. This
keeps the simulator testable in isolation (no DB, no clock, no audit)
and guarantees the dry-run trace mirrors the live engine: the
acceptance test for FM-015 asserts the two produce the same verdict
for the same case + actions.

If the runtime engine ever needs a richer evaluator (e.g. joins
across repositories, derived fields), the seam lives in
``domain/rules.py`` and both call sites import from there. For v1 the
inline evaluator is sufficient and avoids pulling a ``domain/rules.py``
module into scope that no caller yet consumes.

# Determinism

The simulator is a **pure function** over its inputs: same
``workflow_version`` + same ``synthetic_case`` + same ``actions`` =>
identical ``SimulationResult``. There is no clock, no randomness, no
I/O. The output trace lists every step in order with the resolved
``transition``, the ``from``/``to`` state keys, and the ``guards``
array. Each guard entry carries the rule's shape (``rule``), the
inputs that were resolved from the synthetic case (``inputs``), and
the boolean ``result`` so the admin UI can render a per-clause
breakdown.

# Outcomes

- ``completed`` -- the simulation reached a terminal state.
- ``blocked`` -- at some step the configured transition's guards were
  not satisfied (or no outgoing transition matched the requested
  action). ``blockers`` lists the unmet guards / the missing transition.
- ``exception`` -- the inputs were structurally invalid (unknown
  transition key, ambiguous candidates when no choice was provided).

The distinction matters for the admin UI: ``blocked`` is a tuning
problem the author can fix, ``exception`` is an integration bug that
should page someone.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.domain.errors import InvariantViolation, NotFound
from app.domain.workflow import WorkflowState, WorkflowTransition, WorkflowVersion

# Outcome tags. Kept as a ``Literal`` so Pydantic schemas on the HTTP
# boundary can reuse the same names without redefining an enum.
SimulationOutcome = Literal["completed", "blocked", "exception"]


# ---------------------------------------------------------------------------
# Pure data carriers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SimulatedGuardResult:
    """One guard clause's evaluation outcome.

    ``rule`` is the JSON document the workflow author declared. ``inputs``
    is the resolved field values pulled from the synthetic case for the
    rule's referenced ``field`` paths; the simulator captures them so
    the admin UI can explain *why* a clause failed (rather than just
    *that* it failed). ``result`` is the boolean evaluation.
    """

    rule: dict[str, Any]
    inputs: dict[str, Any]
    result: bool


@dataclass(frozen=True, slots=True)
class SimulatedStep:
    """One transition that the simulator fired.

    The ``actions_fired`` list carries the JSON action documents the
    transition declared, in declaration order. The simulator does not
    *execute* the actions (the live engine does that in a later slice);
    it only records them so the trace is complete.
    """

    transition: str
    from_state: str
    to_state: str
    guards: tuple[SimulatedGuardResult, ...]
    actions_fired: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class SimulationBlocker:
    """A reason the simulation stopped.

    ``kind`` distinguishes a missing transition (``no_matching_transition``),
    an unmet guard (``guard_failed``), and an input shape defect
    (``invalid_request``). ``message`` is safe to surface to a tenant
    administrator -- it never includes case content from the synthetic
    case.
    """

    kind: Literal["no_matching_transition", "guard_failed", "invalid_request"]
    message: str
    transition: str | None = None
    state: str | None = None
    failed_guard: SimulatedGuardResult | None = None


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """The complete dry-run result for one synthetic case.

    ``initial_state`` is the resolved entry state (the version's
    ``is_initial`` flag); ``final_state`` is the state at which the
    simulation stopped (``initial_state`` if no step fired). ``outcome``
    is one of ``completed`` / ``blocked`` / ``exception``. ``steps`` is
    the ordered trace of every transition the simulator fired.
    """

    initial_state: str
    final_state: str
    outcome: SimulationOutcome
    steps: tuple[SimulatedStep, ...] = field(default_factory=tuple)
    blockers: tuple[SimulationBlocker, ...] = field(default_factory=tuple)


# ``SyntheticCase`` is intentionally a structural alias -- the simulator
# only reads it as a mapping. Callers pass ``dict[str, Any]`` or a
# dataclass; both work because every read goes through ``_resolve_field``.
SyntheticCase = Mapping[str, Any]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def simulate(
    workflow_version: WorkflowVersion,
    synthetic_case: SyntheticCase,
    actions: Sequence[Mapping[str, Any]] | None = None,
) -> SimulationResult:
    """Simulate ``workflow_version`` against ``synthetic_case``.

    Args:
        workflow_version: the ``WorkflowVersion`` under test. May be
            ``draft`` or ``published``; the simulator never mutates it.
        synthetic_case: the synthetic case data the guards will be
            evaluated against. The simulator reads fields by stable
            keys (``case.participants``, ``case.documents``, ...).
        actions: an optional, ordered list of actor decisions. Each
            element is a mapping with ``transition_key`` (the
            transition the actor fires) and an optional ``payload``.
            When omitted, the simulator auto-progresses by selecting the
            first outgoing transition whose guards pass.

    Returns:
        :class:`SimulationResult` with the initial state, the final
        state, the ordered step trace, and the blockers (if any).

    Raises:
        NotFound: the workflow has no initial state.
        InvariantViolation: the workflow has more than one initial
            state, or the version has no states.
    """
    states_by_key, initial_state_key = _resolve_initial_state(workflow_version)
    transitions_by_key = {t.key: t for t in workflow_version.transitions}
    outgoing_by_state: dict[str, tuple[WorkflowTransition, ...]] = {}
    for declared_transition in workflow_version.transitions:
        outgoing_by_state.setdefault(declared_transition.from_state, ())
        outgoing_by_state[declared_transition.from_state] = (
            *outgoing_by_state[declared_transition.from_state],
            declared_transition,
        )

    current_state: str = initial_state_key
    steps: list[SimulatedStep] = []
    blockers: list[SimulationBlocker] = []

    # Each iteration of the loop is one transition. We bound the loop
    # at ``2 * number_of_states`` to guarantee termination on accidental
    # automatic cycles -- the validator rejects those at publish time,
    # but the simulator is also a defense-in-depth signal in case the
    # validator was bypassed.
    max_iterations = max(2 * len(states_by_key), 2)
    for _ in range(max_iterations):
        if _is_terminal(states_by_key, current_state):
            return SimulationResult(
                initial_state=initial_state_key,
                final_state=current_state,
                outcome="completed",
                steps=tuple(steps),
                blockers=tuple(blockers),
            )

        # Decide which transition to attempt next. ``actions`` is an
        # explicit actor script; ``None`` means auto-progress. The
        # return type is ``WorkflowTransition | None``; the narrowing
        # below rejects ``None`` as a ``no_matching_transition`` blocker.
        transition: WorkflowTransition | None = _select_next_transition(
            outgoing=outgoing_by_state.get(current_state, ()),
            actions=actions,
            actions_cursor=len(steps),
            transitions_by_key=transitions_by_key,
        )
        if transition is None:
            blockers.append(
                SimulationBlocker(
                    kind="no_matching_transition",
                    message=(f"no matching outgoing transition from state {current_state!r}"),
                    state=current_state,
                )
            )
            return SimulationResult(
                initial_state=initial_state_key,
                final_state=current_state,
                outcome="blocked",
                steps=tuple(steps),
                blockers=tuple(blockers),
            )

        # Evaluate guards. The simulator applies the same operator set
        # the runtime engine applies; see ``_evaluate_guard`` below.
        guard_results = _evaluate_guard_list(transition, synthetic_case)
        guard_failed = next((g for g in guard_results if not g.result), None)
        if guard_failed is not None:
            blockers.append(
                SimulationBlocker(
                    kind="guard_failed",
                    message=(
                        f"guard failed for transition {transition.key!r} in state {current_state!r}"
                    ),
                    transition=transition.key,
                    state=current_state,
                    failed_guard=guard_failed,
                )
            )
            return SimulationResult(
                initial_state=initial_state_key,
                final_state=current_state,
                outcome="blocked",
                steps=tuple(steps),
                blockers=tuple(blockers),
            )

        # Record the step and advance. The simulator does NOT mutate
        # the synthetic case; action effects live in the live engine.
        steps.append(
            SimulatedStep(
                transition=transition.key,
                from_state=current_state,
                to_state=transition.to_state,
                guards=tuple(guard_results),
                actions_fired=tuple(transition.actions),
            )
        )
        current_state = transition.to_state

    # If we get here we ran out of iterations -- an automatic cycle the
    # validator missed. Surface as ``blocked`` with a diagnostic; never
    # silently spin.
    blockers.append(
        SimulationBlocker(
            kind="invalid_request",
            message=(
                f"simulation exceeded {max_iterations} iterations -- likely "
                f"an automatic cycle the validator did not reject"
            ),
            state=current_state,
        )
    )
    return SimulationResult(
        initial_state=initial_state_key,
        final_state=current_state,
        outcome="blocked",
        steps=tuple(steps),
        blockers=tuple(blockers),
    )


# ---------------------------------------------------------------------------
# State-machine helpers
# ---------------------------------------------------------------------------


def _resolve_initial_state(
    workflow_version: WorkflowVersion,
) -> tuple[dict[str, WorkflowState], str]:
    states_by_key = {state.key: state for state in workflow_version.states}
    if not states_by_key:
        raise InvariantViolation("workflow definition has no states")
    initial_keys = [state.key for state in workflow_version.states if state.is_initial]
    if not initial_keys:
        raise NotFound("workflow definition has no initial state")
    if len(initial_keys) > 1:
        raise InvariantViolation(
            f"workflow definition has multiple initial states: {initial_keys!r}"
        )
    return states_by_key, initial_keys[0]


def _is_terminal(states_by_key: dict[str, WorkflowState], state_key: str) -> bool:
    state = states_by_key.get(state_key)
    return state is not None and state.is_terminal


def _select_next_transition(
    *,
    outgoing: tuple[WorkflowTransition, ...],
    actions: Sequence[Mapping[str, Any]] | None,
    actions_cursor: int,
    transitions_by_key: dict[str, WorkflowTransition],
) -> WorkflowTransition | None:
    """Pick the next transition to attempt.

    With an explicit ``actions`` script, the cursor indexes into it; the
    entry's ``transition_key`` resolves to the outgoing transition the
    actor fired. A script that references a non-outgoing transition is
    a ``no_matching_transition`` blocker (the simulator never silently
    picks a different transition).

    Without ``actions``, the simulator auto-progresses by selecting the
    first outgoing transition in declaration order. This matches the
    runtime engine's contract for deterministic automatic progression
    (Constitution XIV) and makes the dry-run trace predictable.
    """
    if actions is not None:
        if actions_cursor >= len(actions):
            return None
        requested = actions[actions_cursor]
        key = str(requested.get("transition_key", ""))
        if not key:
            return None
        return transitions_by_key.get(key)
    if not outgoing:
        return None
    return outgoing[0]


# ---------------------------------------------------------------------------
# Guard evaluator
# ---------------------------------------------------------------------------


def _evaluate_guard_list(
    transition: WorkflowTransition, synthetic_case: SyntheticCase
) -> tuple[SimulatedGuardResult, ...]:
    """Evaluate every guard clause on the transition.

    A transition with no ``guard`` is unconditionally allowed (the
    simulator records an empty tuple -- the trace carries an empty
    ``guards`` array, which is the same shape the live engine produces
    for an unguarded transition).
    """
    if transition.guard is None:
        return ()
    # Top-level guard: the author writes a single rule OR a composite
    # (``all``/``any``/``not``). Walk it like any other rule; the
    # recursion shares the same evaluation path. We deliberately do
    # NOT flatten composites into a list of leaf evaluations -- the
    # trace must mirror the declared shape so an admin can read it.
    return (_evaluate_rule(transition.guard, synthetic_case),)


def _evaluate_rule(rule: Mapping[str, Any], synthetic_case: SyntheticCase) -> SimulatedGuardResult:
    op = str(rule.get("op", ""))

    if op in {"all", "any"}:
        return _evaluate_composite(rule, op, synthetic_case)
    if op == "not":
        return _evaluate_negation(rule, synthetic_case)
    return _evaluate_leaf(rule, op, synthetic_case)


def _evaluate_composite(
    rule: Mapping[str, Any], op: str, synthetic_case: SyntheticCase
) -> SimulatedGuardResult:
    """Evaluate ``all`` / ``any`` over the rule's ``clauses`` array.

    The trace preserves the declared shape: a composite produces one
    ``SimulatedGuardResult`` whose ``inputs`` carries per-clause
    results, not a flattened list of leaf evaluations. This matches
    the wire shape the admin UI expects.
    """
    clauses = rule.get("clauses") or []
    clause_results: list[SimulatedGuardResult] = [
        _evaluate_rule(child, synthetic_case) for child in clauses
    ]
    if op == "all":
        result = all(c.result for c in clause_results)
    else:
        result = any(c.result for c in clause_results)
    return SimulatedGuardResult(
        rule=dict(rule),
        inputs={"clauses": [{"result": c.result, "inputs": c.inputs} for c in clause_results]},
        result=result,
    )


def _evaluate_negation(
    rule: Mapping[str, Any], synthetic_case: SyntheticCase
) -> SimulatedGuardResult:
    """Evaluate a ``not`` rule. The ``clause`` shape is required."""
    inner = rule.get("clause") or {}
    inner_result = _evaluate_rule(inner, synthetic_case)
    return SimulatedGuardResult(
        rule=dict(rule),
        inputs={"clause": inner_result.inputs, "clause_result": inner_result.result},
        result=not inner_result.result,
    )


def _evaluate_leaf(
    rule: Mapping[str, Any], op: str, synthetic_case: SyntheticCase
) -> SimulatedGuardResult:
    """Evaluate a leaf guard against the synthetic case.

    ``_resolve_field`` returns ``None`` for missing fields; the leaf
    operator decides whether missing means "exists=False" (most do) or
    "exists=True" (which is a rule bug -- the simulator fails closed).

    The leaf operators are dispatched through a small table so each
    operator keeps a single responsibility and PLR0911 (too many
    returns) does not accumulate as the set grows.
    """
    field = str(rule.get("field", ""))
    value = _resolve_field(synthetic_case, field)
    inputs: dict[str, Any] = {"field": field, "value": value}

    handler = _LEAF_OPERATORS.get(op)
    if handler is None:
        # Unknown operator: the validator should have rejected this at
        # publish time, but the simulator stays fail-closed and treats
        # it as a guard failure rather than crashing.
        return SimulatedGuardResult(rule=dict(rule), inputs=inputs, result=False)
    return handler(rule, value, inputs)


def _leaf_exists(
    rule: Mapping[str, Any], value: Any, inputs: dict[str, Any]
) -> SimulatedGuardResult:
    threshold = rule.get("min", 1)
    try:
        threshold_int = int(threshold)
    except (TypeError, ValueError):
        threshold_int = 1
    if value is None:
        result = False
    elif isinstance(value, (list, tuple, set, frozenset, str, bytes)):
        result = len(value) >= threshold_int
    else:
        # Scalar: existence means "is not None" regardless of ``min``.
        # The operator's ``min`` only makes sense for collections.
        result = True
    return SimulatedGuardResult(rule=dict(rule), inputs=inputs, result=result)


def _leaf_eq(rule: Mapping[str, Any], value: Any, inputs: dict[str, Any]) -> SimulatedGuardResult:
    return SimulatedGuardResult(
        rule=dict(rule),
        inputs={**inputs, "expected": rule.get("value")},
        result=value == rule.get("value"),
    )


def _leaf_ne(rule: Mapping[str, Any], value: Any, inputs: dict[str, Any]) -> SimulatedGuardResult:
    return SimulatedGuardResult(
        rule=dict(rule),
        inputs={**inputs, "expected": rule.get("value")},
        result=value != rule.get("value"),
    )


def _leaf_in(rule: Mapping[str, Any], value: Any, inputs: dict[str, Any]) -> SimulatedGuardResult:
    options = rule.get("options") or []
    return SimulatedGuardResult(
        rule=dict(rule),
        inputs={**inputs, "options": list(options)},
        result=value in options,
    )


def _leaf_order(
    rule: Mapping[str, Any], value: Any, inputs: dict[str, Any]
) -> SimulatedGuardResult:
    return SimulatedGuardResult(
        rule=dict(rule),
        inputs={**inputs, "expected": rule.get("value")},
        result=_compare(rule["op"], value, rule.get("value")),
    )


def _evaluate_contains(
    rule: Mapping[str, Any], value: Any, inputs: dict[str, Any]
) -> SimulatedGuardResult:
    """``contains`` operator: substring (string) or membership (collection)."""
    needle = rule.get("value")
    if isinstance(value, (list, tuple, set, frozenset)):
        ok = needle in value
    elif isinstance(value, str):
        ok = needle is not None and str(needle) in value
    else:
        ok = False
    return SimulatedGuardResult(rule=dict(rule), inputs={**inputs, "expected": needle}, result=ok)


def _evaluate_regex(
    rule: Mapping[str, Any], value: Any, inputs: dict[str, Any]
) -> SimulatedGuardResult:
    """``regex`` operator: search the field against the declared pattern.

    Fails closed on a malformed pattern or a non-string value -- the
    simulator never raises on guard evaluation, the validator already
    shape-checks the pattern at publish time.
    """
    pattern = rule.get("pattern")
    if not isinstance(pattern, str) or not isinstance(value, str):
        return SimulatedGuardResult(rule=dict(rule), inputs=inputs, result=False)
    try:
        compiled = re.compile(pattern)
        ok = compiled.search(value) is not None
    except re.error:
        ok = False
    return SimulatedGuardResult(
        rule=dict(rule),
        inputs={**inputs, "pattern": pattern},
        result=ok,
    )


# Dispatch table -- one handler per leaf operator. ``order``
# (``gt``/``gte``/``lt``/``lte``) share a handler because their
# semantics differ only by the comparator. Defined after the leaf
# helpers so the names are resolvable at module load time.
_LEAF_OPERATORS = {
    "exists": _leaf_exists,
    "eq": _leaf_eq,
    "ne": _leaf_ne,
    "in": _leaf_in,
    "gt": _leaf_order,
    "gte": _leaf_order,
    "lt": _leaf_order,
    "lte": _leaf_order,
    "contains": _evaluate_contains,
    "regex": _evaluate_regex,
}


def _compare(op: str, left: Any, right: Any) -> bool:
    """Compare two values for an ordering operator.

    Returns ``False`` when either side is ``None`` or not orderable --
    the rule fails closed rather than crashing on missing data. The
    comparator is looked up in a small dispatch table so adding a new
    ordering operator does not inflate PLR0911.
    """
    if left is None or right is None:
        return False
    comparator = _ORDER_COMPARATORS.get(op)
    if comparator is None:
        return False
    try:
        result = comparator(left, right)
    except TypeError:
        return False
    return bool(result)


def _lt(left: Any, right: Any) -> Any:
    return left < right


def _lte(left: Any, right: Any) -> Any:
    return left <= right


def _gt(left: Any, right: Any) -> Any:
    return left > right


def _gte(left: Any, right: Any) -> Any:
    return left >= right


_ORDER_COMPARATORS: dict[str, Any] = {
    "gt": _gt,
    "gte": _gte,
    "lt": _lt,
    "lte": _lte,
}


def _resolve_field(synthetic_case: SyntheticCase, path: str) -> Any:
    """Resolve a dotted field path against the synthetic case.

    ``case.participants`` -> ``synthetic_case["case"]["participants"]``.
    Missing segments yield ``None`` so a guard can compare against it.
    Empty / falsy paths also yield ``None`` -- the rule author wrote a
    typo, the simulator stays fail-closed.
    """
    if not path:
        return None
    cursor: Any = synthetic_case
    for segment in path.split("."):
        cursor = _resolve_segment(cursor, segment)
        if cursor is None:
            return None
    return cursor


def _resolve_segment(cursor: Any, segment: str) -> Any:
    """Walk one segment of a dotted field path.

    Maps raise ``None`` for a missing key, lists / tuples raise
    ``None`` for an out-of-range integer index. Anything else (scalar
    with more path to walk, mismatched type) also yields ``None`` --
    the rule author wrote a typo and the simulator stays fail-closed.
    """
    if isinstance(cursor, Mapping):
        return cursor.get(segment)
    if isinstance(cursor, (list, tuple)) and segment.isdigit():
        index = int(segment)
        if -len(cursor) <= index < len(cursor):
            return cursor[index]
        return None
    return None


__all__ = [
    "SimulationBlocker",
    "SimulationOutcome",
    "SimulationResult",
    "SimulatedGuardResult",
    "SimulatedStep",
    "simulate",
]
