"""Shared command/result types, the guard error, and the pure parallel join.

Split from the flat ``execute_transition`` module (R2: files under 600 lines,
target under 500). This module is the leaf of the package — it depends only
on the domain layer. The package ``__init__`` re-exports every name so the
import surface of ``app.application.workflow.execute_transition`` is
identical to the pre-split module (including private helpers used by
``preflight_transitions``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.case import Case
from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.task import Task
from app.domain.workflow import JoinMode, WorkflowTransition

# Stable subject / event-type strings; subscribers depend on them.
SUBJECT_TYPE_CASE = "case"
SUBJECT_TYPE_WORKFLOW_INSTANCE = "workflow_instance"


class _ClockLike(Protocol):
    """Minimal clock protocol used by :func:`handle`."""

    def now(self) -> datetime: ...


class GuardBlocked(ValidationFailed):
    """A workflow guard rejected the transition (FR-010).

    The use case raises this when :func:`app.domain.rules.evaluate`
    returns ``False`` or when the guard document is structurally
    invalid. The HTTP boundary maps :class:`ValidationFailed` to a 422
    ``validation_failed`` envelope; ``GuardBlocked`` carries an
    additional ``reason`` field that names the unmet requirement
    (``missing_documents``, ``missing_participant``, ``missing_field``,
    ``rule_unmet``, ``guard_invalid``) so the client can render a
    targeted message without re-running the workflow.

    Why a subclass of ``ValidationFailed``: the HTTP layer's existing
    422 mapping is the correct status code for "your request was
    structurally fine but the workflow said no"; ``Forbidden`` would be
    wrong (the actor has the action grant) and ``Conflict`` would
    misrepresent the failure (the case state is not stale).
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, field=field)
        self.reason = reason
        self.details: dict[str, Any] = details or {}


@dataclass(frozen=True, slots=True)
class ExecuteTransitionCommand:
    """Input to :func:`handle`.

    ``transition_key`` is the stable key declared on the
    ``WorkflowTransition`` (``"submit_for_review"``, ``"record_decision"``).
    ``payload`` is the caller's transition payload (decision, notes,
    references); it is recorded on the audit row but does not change
    the guard evaluation — guards see the case context, not the
    payload.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    transition_key: str
    payload: dict[str, Any] | None = None
    correlation_id: str = ""
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecuteTransitionResult:
    """Post-commit outcome returned to the router / caller.

    ``case`` carries the post-transition state (``current_workflow_state``
    bumped). ``tasks`` is the list of new Tasks materialized by the
    transition's ``create_task`` actions. ``transitioned_at`` is the
    timestamp stamped on the audit + outbox rows.
    """

    case: Case
    instance_id: UUID
    from_state: str
    to_state: str
    transition_key: str
    tasks: list[Task] = field(default_factory=list)
    transitioned_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JoinDecision:
    """Deterministic result of evaluating a parallel workflow join.

    ``completed_branches`` contains only declared branch keys, in the
    declaration order from ``WorkflowTransition.branches``. ``next_state``
    is the join target only when ``fire`` is true; otherwise it is ``None``.
    """

    fire: bool
    next_state: str | None
    completed_branches: tuple[str, ...]


def evaluate_join(
    transition: WorkflowTransition,
    *,
    completed_branch_keys: Iterable[str],
) -> JoinDecision:
    """Evaluate whether a parallel transition has reached its join threshold.

    Unknown and duplicate completion keys are ignored. The declared branch
    order is used for the returned tuple so audit and retry behavior do not
    depend on event arrival order. ``all`` requires every branch, ``any``
    requires one branch, and ``count`` requires ``min_completions`` branches.

    Raises:
        InvariantViolation: If the transition is not a valid parallel join.
    """
    if not transition.branches or transition.join is None:
        raise InvariantViolation(f"transition {transition.key!r} does not declare a parallel join")

    declared_keys = tuple(branch.branch_key for branch in transition.branches)
    completed_keys = frozenset(completed_branch_keys)
    completed_branches = tuple(key for key in declared_keys if key in completed_keys)
    join = transition.join

    if join.mode is JoinMode.ALL:
        threshold = len(declared_keys)
    elif join.mode is JoinMode.ANY:
        threshold = 1
    elif join.mode is JoinMode.COUNT:
        if join.min_completions is None or not 1 <= join.min_completions <= len(declared_keys):
            raise InvariantViolation(
                f"transition {transition.key!r} has an invalid count join threshold"
            )
        threshold = join.min_completions
    else:
        raise InvariantViolation(f"transition {transition.key!r} has an unsupported join mode")

    fire = len(completed_branches) >= threshold
    return JoinDecision(
        fire=fire,
        next_state=join.target_state if fire else None,
        completed_branches=completed_branches,
    )
