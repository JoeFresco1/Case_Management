"""Graph analyses: reachability and automatic-cycle detection (plan §5).

Workflow validation is largely graph theory over the declared state
machine. This module owns every graph walk:

- :func:`_validate_forward_reachability` — every declared state must
  be reachable from the initial state (no dead authoring).
- :func:`_validate_terminal_reachability` — every non-terminal state
  must be able to reach at least one terminal state, otherwise a case
  could be trapped forever.
- :func:`_validate_no_automatic_cycle` — a cycle composed entirely of
  ``human_driven=False`` transitions with no breakable guard is
  rejected as a potential runaway; a cycle containing at least one
  ``human_driven=True`` transition is **allowed** (plan §5:
  "Human-driven workflow cycles are allowed"). SCC detection uses
  Tarjan's algorithm (:func:`_strongly_connected_components`) and the
  error names the blocker transitions inside the cycle.
- :func:`_can_reach` / :func:`_can_reach_terminal` — small BFS helpers
  shared with the parallel-join validator.

Pure functions: no I/O, deterministic. Split from the pre-R2 single-file
validator (``validate_workflow_definition.py``) by responsibility.
"""

from __future__ import annotations

from app.domain.errors import ValidationFailed
from app.domain.workflow import (
    WorkflowState,
    WorkflowVersion,
)


def _validate_forward_reachability(
    version: WorkflowVersion,
    states_by_key: dict[str, WorkflowState],
) -> None:
    """Reject states no case can ever enter.

    A state with no forward path from the initial state is dead
    authoring: cases start at ``is_initial`` and only follow declared
    edges, so such a state (and any transition out of it) can never
    execute. The engine would carry dead weight and the admin UI
    would display phantom states.

    Args:
        version: the draft workflow version under review.
        states_by_key: declared states indexed by key.

    Raises:
        ValidationFailed: no initial state, or some state is
            unreachable from the initial state.
    """
    initials = [s.key for s in version.states if s.is_initial]
    if not initials:
        # No initial state: `_validate_states` already rejected this;
        # fail closed here too so the BFS below has a start.
        raise ValidationFailed(
            "workflow definition requires an initial state",
            field="states",
        )
    adjacency: dict[str, set[str]] = {key: set() for key in states_by_key}
    for transition in version.transitions:
        if transition.from_state in adjacency and transition.to_state in adjacency:
            adjacency[transition.from_state].add(transition.to_state)
        # Parallel fan-out enters branch states without a top-level
        # edge: the `branches` spec IS the entry (and the branch runs
        # to its terminal). Without these edges every parallel branch
        # past the first would look unreachable.
        for branch in transition.branches or ():
            if transition.from_state in adjacency and branch.from_state in adjacency:
                adjacency[transition.from_state].add(branch.from_state)
            if branch.from_state in adjacency and branch.terminal_state in adjacency:
                adjacency[branch.from_state].add(branch.terminal_state)
    visited: set[str] = set()
    stack: list[str] = list(initials)
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        for nxt in adjacency.get(current, ()):
            if nxt not in visited:
                stack.append(nxt)
    for key in states_by_key:
        if key not in visited:
            raise ValidationFailed(
                f"state {key!r} is unreachable from the initial state",
                field=f"states.{key}",
            )


def _validate_terminal_reachability(
    version: WorkflowVersion,
    states_by_key: dict[str, WorkflowState],
) -> None:
    """Reject states that can never finish.

    Every non-terminal state must reach *some* terminal state via
    forward edges (the engine only follows forward edges). A state
    with no such path would trap the case.

    Args:
        version: the draft workflow version under review.
        states_by_key: declared states indexed by key.

    Raises:
        ValidationFailed: no terminal state at all, or a non-terminal
            state cannot reach any terminal state.
    """
    terminal_states = {s.key for s in version.states if s.is_terminal}
    if not terminal_states:
        raise ValidationFailed(
            "workflow definition requires at least one terminal state",
            field="states",
        )

    # Adjacency map: from_state -> {to_state, ...}. We treat the graph
    # as undirected for reachability of terminal states because we want
    # to confirm every non-terminal state can reach *some* terminal
    # state via forward edges (the engine only follows forward edges).
    adjacency: dict[str, set[str]] = {key: set() for key in states_by_key}
    for transition in version.transitions:
        adjacency[transition.from_state].add(transition.to_state)

    for state in version.states:
        if state.is_terminal:
            continue
        if not _can_reach_terminal(state.key, adjacency, terminal_states):
            raise ValidationFailed(
                f"state {state.key!r} cannot reach any terminal state",
                field=f"states.{state.key}",
            )


def _can_reach_terminal(
    start: str,
    adjacency: dict[str, set[str]],
    terminal_states: set[str],
) -> bool:
    """BFS from ``start`` looking for any state in ``terminal_states``.

    Args:
        start: state key to start from.
        adjacency: from_state -> {to_state, ...} forward edges.
        terminal_states: candidate accepting state keys.

    Returns:
        True when some terminal state is reachable.
    """
    visited: set[str] = set()
    stack: list[str] = [start]
    while stack:
        current = stack.pop()
        if current in terminal_states:
            return True
        if current in visited:
            continue
        visited.add(current)
        for nxt in adjacency.get(current, ()):
            if nxt not in visited:
                stack.append(nxt)
    return False


def _validate_no_automatic_cycle(
    version: WorkflowVersion,
    states_by_key: dict[str, WorkflowState],
) -> None:
    """Reject a cycle composed entirely of automatic transitions.

    A cycle is a set of states ``S = {s1, …, sn}`` such that every
    state in ``S`` has at least one outgoing transition whose ``to``
    state is also in ``S``. A cycle is "automatic" if **every**
    transition that participates in it has ``human_driven=False``. The
    validator rejects automatic cycles — they have no breakable guard
    (no human, no external event, no timer) and would trap a case.

    The detection is intentionally local: we look at the cycle subgraph
    induced by the definition and ask whether *every* edge inside it
    is human-driven. If any edge is human-driven, the cycle is
    breakable and the definition is accepted.

    Args:
        version: the draft workflow version under review.
        states_by_key: declared states indexed by key.

    Raises:
        ValidationFailed: an automatic cycle exists; the message names
            the blocker transitions inside it.
    """
    # Adjacency map of "automatic-only" edges: edges with
    # human_driven=False.
    automatic_edges: dict[str, set[str]] = {key: set() for key in states_by_key}
    for transition in version.transitions:
        if not transition.human_driven:
            automatic_edges[transition.from_state].add(transition.to_state)

    # Find strongly connected components (SCCs) of size >= 2 in the
    # automatic-only subgraph. A single-node SCC cannot be a cycle by
    # itself (transitions are not self-loops per WorkflowTransition
    # invariants).
    sccs = _strongly_connected_components(automatic_edges)
    for component in sccs:
        if len(component) < 2:
            continue
        # Confirm at least one edge stays inside the component (the SCC
        # may be a "bridge" of length-2 with edges going out only).
        has_internal_edge = False
        for node in component:
            for nxt in automatic_edges[node]:
                if nxt in component:
                    has_internal_edge = True
                    break
            if has_internal_edge:
                break
        if not has_internal_edge:
            continue
        # Name the blocker transitions inside the cycle so the author
        # knows exactly which edges must gain a human break (T4
        # acceptance: "rejected at publish with named blocker"). Only
        # automatic edges internal to the component are listed — those
        # are the runaway path.
        blockers = sorted(
            t.key
            for t in version.transitions
            if not t.human_driven and t.from_state in component and t.to_state in component
        )
        raise ValidationFailed(
            "workflow has a non-terminating automatic cycle among states "
            f"{sorted(component)!r} via automatic transition(s) "
            f"{blockers!r}; mark at least one as human_driven "
            "(or add a timer/guard break) to allow publication",
            field="transitions",
        )


def _strongly_connected_components(
    adjacency: dict[str, set[str]],
) -> list[set[str]]:
    """Tarjan's SCC algorithm. Returns one component per node-set.

    Workflow definitions are small (plan §5: a workflow is a handful of
    states), so recursion is well within the default Python limit. The
    algorithm runs once at publish time, never on the hot path.

    Args:
        adjacency: node -> {successor, ...} edge map.

    Returns:
        One frozenset-like ``set`` of node keys per strongly connected
        component.
    """
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    result: list[set[str]] = []

    def strongconnect(node: str) -> None:
        indices[node] = index_counter[0]
        lowlinks[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for successor in sorted(adjacency.get(node, ())):
            if successor not in indices:
                strongconnect(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] == indices[node]:
            component: set[str] = set()
            while True:
                successor = stack.pop()
                on_stack.discard(successor)
                component.add(successor)
                if successor == node:
                    break
            result.append(component)

    for node in sorted(adjacency):
        if node not in indices:
            strongconnect(node)
    return result


def _can_reach(
    start: str,
    adjacency: dict[str, set[str]],
    target: str,
) -> bool:
    """BFS from ``start`` looking for ``target``. Pure, deterministic.

    Args:
        start: state key to start from.
        adjacency: from_state -> {to_state, ...} forward edges.
        target: state key to find.

    Returns:
        True when ``target`` is reachable from ``start``.
    """
    if start == target:
        return True
    visited: set[str] = {start}
    stack: list[str] = [start]
    while stack:
        current = stack.pop()
        for nxt in adjacency.get(current, ()):
            if nxt == target:
                return True
            if nxt not in visited:
                visited.add(nxt)
                stack.append(nxt)
    return False
