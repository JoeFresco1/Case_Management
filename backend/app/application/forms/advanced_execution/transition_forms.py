"""State-specific workflow form validation for transitions (P07-US7).

A pure-function guard the workflow engine asks before a transition can
fire: every form bound to the *target* state and marked required must
already be submitted (or the transition is blocked). No I/O — the
engine resolves the binding requirements and the submitted form keys
and owns the commit; this module only returns the verdict.

Composes with :func:`app.application.workflow.execute_transition.handle`
without coupling to its internals (the runtime asks "would this
transition be allowed under the form rules?"; the engine owns the
commit).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.domain.errors import ValidationFailed
from app.domain.form import FormBinding
from app.domain.workflow import WorkflowTransition

# ---------------------------------------------------------------------------
# State-specific workflow form validation (P07-US7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateFormRequirement:
    """One state-bound form requirement declared on a :class:`FormBinding`.

    A binding may declare one or more ``state_forms`` entries — each
    pins a form to a workflow ``state_key`` and declares whether the
    form is ``required`` for a transition that lands in that state.
    This is the seam the workflow engine asks
    :func:`validate_transition_forms` to enforce.
    """

    form_key: str
    state_key: str
    required: bool = True


def derive_state_requirements(
    binding: FormBinding,
) -> tuple[StateFormRequirement, ...]:
    """Resolve the per-state form requirements declared on a binding.

    The binding carries an optional ``state_forms`` list
    (``[{"form_key": "...", "state_key": "...", "required": true},
    ...]``). Missing or empty -> no state-form requirements, which the
    workflow engine treats as "transition is allowed regardless of
    state-bound forms".
    """
    raw = getattr(binding, "state_forms", None) or ()
    if not raw:
        return ()
    requirements: list[StateFormRequirement] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        form_key = str(entry.get("form_key") or "").strip()
        state_key = str(entry.get("state_key") or "").strip()
        if not form_key or not state_key:
            continue
        requirements.append(
            StateFormRequirement(
                form_key=form_key,
                state_key=state_key,
                required=bool(entry.get("required", True)),
            )
        )
    return tuple(requirements)


@dataclass(frozen=True, slots=True)
class TransitionFormCheckResult:
    """The verdict returned by :func:`validate_transition_forms`.

    ``allowed`` is the boolean decision; ``missing`` is the set of
    form_key values whose required submissions are absent for the
    transition's target state. The shape is intentionally narrow so
    the workflow engine can merge it with its own
    ``GuardBlocked``/``Forbidden`` taxonomy without leaking the form
    pipeline into the transition code path.
    """

    allowed: bool
    missing: tuple[str, ...] = ()
    evaluated_requirements: tuple[StateFormRequirement, ...] = ()


def validate_transition_forms(
    *,
    transition: WorkflowTransition,
    target_state_requirements: Iterable[StateFormRequirement],
    submitted_form_keys: Iterable[str],
    declared_states: Iterable[str] | None = None,
) -> TransitionFormCheckResult:
    """Validate that every state-bound form has been submitted for ``transition``.

    P07-US7: a transition that lands in a state with required
    state-bound forms is rejected when any of those forms has not been
    submitted yet. The check is *deterministic* and *pure*: the
    workflow engine passes the requirements it resolved from the
    binding plus the set of form_keys the case already has submitted
    forms for; the function returns a verdict. No DB access here —
    the engine owns the lookup seam.

    The transition's ``to_state`` is what we look at, not ``from``.
    A reviewer who lands in ``in_review`` must have submitted the
    state-bound review form; the outgoing transition is unaffected
    because the *next* transition's target state may carry different
    requirements.

    When ``declared_states`` is supplied the function rejects
    requirements whose ``state_key`` is not in the set (a misconfigured
    binding that names a state the workflow does not have is treated
    as a hard configuration error so the operator sees it during the
    next validation pass rather than as silent drift at runtime).
    """
    requirements = tuple(target_state_requirements)
    if not requirements:
        return TransitionFormCheckResult(allowed=True)
    if declared_states is not None:
        declared = frozenset(declared_states)
        for requirement in requirements:
            if requirement.state_key not in declared:
                raise ValidationFailed(
                    f"state-form requirement references undeclared state {requirement.state_key!r}",
                    field="state_key",
                )
    target_state = transition.to_state
    relevant = tuple(r for r in requirements if r.state_key == target_state and r.required)
    if not relevant:
        return TransitionFormCheckResult(
            allowed=True,
            evaluated_requirements=relevant,
        )
    submitted = frozenset(submitted_form_keys)
    missing = tuple(
        requirement.form_key for requirement in relevant if requirement.form_key not in submitted
    )
    return TransitionFormCheckResult(
        allowed=not missing,
        missing=missing,
        evaluated_requirements=relevant,
    )
