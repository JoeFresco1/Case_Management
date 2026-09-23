"""Validate a draft ``WorkflowVersion`` before publication (FM-013 / FR-015).

# Why a package (R2 structural split)

Formerly a single ``validate_workflow_definition.py`` module of ~914
lines — past the R2 600-line soft limit. Split by responsibility into
:mod:`guards` (declarative guard documents), :mod:`actions`
(action/SLA/escalation shape), :mod:`graph` (forward/terminal
reachability and automatic-cycle SCC detection), and :mod:`structure`
(states, transitions, approvals, subprocesses, parallel joins). This
``__init__`` is a pure re-export shim: ``__all__`` and every previously
visible attribute are re-exported unchanged, so no consumer import line
or patch target needs editing.

# What the validator enforces

The validator is the **publish gate** for workflow definitions. It
enforces the structural and semantic rules from Pillar P02 §1 and the
first-slice plan §5:

- **Reachability**: every declared state is reachable from the initial
  state via some chain of transitions (:mod:`graph`).
- **Single initial state**: exactly one state has ``is_initial=True``
  (:mod:`structure`).
- **Ambiguity**: at most one transition may originate from any single
  ``from_state`` with the same ``key`` (duplicate transition keys are
  rejected). The state machine is unambiguous by design — each
  transition is identified by its ``key``; the engine that picks a
  transition given the current state is a later slice, so v1 simply
  rejects collisions.
- **Unknown references**: every transition's ``from`` and ``to`` must
  match a declared state; every ``actions[].queue`` reference must
  match a known queue (provided to the validator; v1 reads queues from
  the repository in the publish use case — here we accept the set so
  the validator is pure).
- **Terminal reachability**: every non-terminal state must be able to
  reach at least one terminal state via transitions. A state with no
  possible exit (excluding terminal states) is rejected — it would trap
  the case (plan §5: "States with no possible exit (excluding terminal
  states) → rejected").
- **Guard operator**: the optional declarative ``guard`` document must
  use a known operator (the same set as CaseType rules; Pillar P05 §2)
  and pass the same shape checks as the case-type rule validator
  (:mod:`guards`).
- **Automatic cycle rejection**: a cycle composed entirely of
  ``human_driven=False`` transitions with no breakable guard is
  rejected as a potential runaway. A cycle that contains at least one
  ``human_driven=True`` transition is **allowed** — a human (or
  external event) can break the loop (plan §5: "Human-driven workflow
  cycles are allowed").
- **Missing terminal**: a workflow with no terminal state at all is
  rejected. Every workflow must have a way to "finish" (closed,
  decided, archived, …).

The validator is a **pure function**: no I/O, no time, no randomness.
The publish use case and the unit test suite both call it directly.
Defects raise :class:`app.domain.errors.ValidationFailed` with an
optional ``field=`` marker; the HTTP boundary maps ``ValidationFailed``
to ``422 validation_failed``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any  # noqa: F401

from app.domain.errors import ValidationFailed  # noqa: F401
from app.domain.workflow import JoinMode, WorkflowState, WorkflowVersion  # noqa: F401

from .actions import (  # noqa: F401
    _ALLOWED_ACTION_TYPES,
    _check_queue_known,
    _validate_actions,
    _validate_escalation,
    _validate_exception_state_escalation,
    _validate_queue_move_escalation,
    _validate_start_sla_action,
    _validate_supervisor_task_escalation,
)
from .graph import (  # noqa: F401
    _can_reach,
    _can_reach_terminal,
    _strongly_connected_components,
    _validate_forward_reachability,
    _validate_no_automatic_cycle,
    _validate_terminal_reachability,
)
from .guards import (  # noqa: F401
    _ALLOWED_GUARD_OPERATORS,
    _MAX_GUARD_NESTING_DEPTH,
    _ensure_authoring_shape,
    _to_authoring_shape,
    _validate_guard_shape,
)
from .structure import (  # noqa: F401
    _validate_approvals,
    _validate_parallel,
    _validate_states,
    _validate_subprocesses,
    _validate_transitions,
)

# why annotated: the pre-split module had module-level annotated constants,
# so its attribute surface included ``__annotations__``; annotating ``__all__``
# keeps the package's ``dir()`` byte-identical to the old module (R2 parity).
__all__: list[str] = [
    "validate_definition",
    "_ALLOWED_ACTION_TYPES",
    "_ALLOWED_GUARD_OPERATORS",
]


def validate_definition(
    version: WorkflowVersion,
    *,
    known_queues: Iterable[str] = (),
    known_classifications: Iterable[str] = (),
) -> None:
    """Validate a draft ``WorkflowVersion``. Raises on the first defect.

    Runs each structural phase in dependency order: declaration shape
    first, then graph analyses over the indexed states, then actions.

    Args:
        version: the draft workflow version under review.
        known_queues: the set of queue keys the workflow may reference
            in its transition ``actions``. v1 callers pass the queues
            the tenant has already configured; the validator is pure
            and does not look them up itself.
        known_classifications: the set of document classification labels
            the workflow author may reference in a document-requirements
            guard (``{"op": "exists", "field":
            "case.documents_by_classification.X", "min": 1,
            "classification": "X"}``). When the iterable is empty the
            cross-check is skipped — useful for unit tests that want to
            exercise the path without seeding a CaseType — but the
            shape check (non-empty string ``classification`` when the
            field references documents_by_classification) is still
            enforced so a malformed guard cannot sneak through.

    Raises:
        ValidationFailed: the definition has at least one defect.
    """
    states_by_key = _validate_states(version.states)
    _validate_subprocesses(version, states_by_key)
    _validate_transitions(version, states_by_key, known_classifications)
    _validate_approvals(version)
    _validate_forward_reachability(version, states_by_key)
    _validate_terminal_reachability(version, states_by_key)
    _validate_no_automatic_cycle(version, states_by_key)
    _validate_actions(version, known_queues)
    _validate_parallel(version, states_by_key)


# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them (and the package-only ``__path__``) keeps
# ``dir(app.application.configuration.validate_workflow_definition)``
# byte-identical to the old module's attribute surface — the R2 refactor must
# be invisible. The submodules stay importable (they are already in
# ``sys.modules``, which the import system consults before touching
# ``__path__``); only the redundant package-level attributes go away.
for _submodule in (
    "actions",
    "graph",
    "guards",
    "structure",
):
    globals().pop(_submodule, None)
del _submodule
globals().pop("__path__", None)
