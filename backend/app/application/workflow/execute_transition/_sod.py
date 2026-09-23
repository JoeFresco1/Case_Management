"""Approval / separation-of-duties helpers for approval-required transitions.

Split from the flat ``execute_transition`` module (R2: files under 600
lines). Pure functions only — no I/O — so the caller can stamp the same
``(outcome, reason)`` pair on the audit row whichever branch it takes
(P02-US4 / FM-017 / P11-US5).
"""

from __future__ import annotations

from uuid import UUID

from app.domain.case import Case
from app.domain.workflow import WorkflowTransition

_SYSTEM_ROLE_MARKER = "system"


def _is_system(actor_roles: tuple[str, ...]) -> bool:
    """Return True iff the actor carries the ``system`` role marker.

    The first-slice ``UserContext`` carries an ``is_system`` flag on the
    real ``app.deps.UserContext`` type. The application layer's duck-typed
    ``_ActorProxy`` does not, so we infer it from the role set: an actor
    whose roles include ``"system"`` is treated as a platform actor
    that may bypass SoD (the outbox dispatcher, SLA scanner, and
    replayable hash-chain verifier all run as ``system``).

    Why a role-based check rather than an explicit flag on the proxy:
    the existing :func:`handle` signature already takes ``actor_roles``
    as a tuple of strings; introducing a new boolean would change
    every call site without buying us anything the role check does not
    already provide.
    """
    return _SYSTEM_ROLE_MARKER in actor_roles


def _evaluate_separation_of_duties(
    *,
    transition: WorkflowTransition,
    case: Case,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
) -> tuple[str, str]:
    """Return ``(outcome, reason)`` for an approval-required transition.

    Outcomes:

    - ``"allowed"`` — the actor is a different identity from the
      case originator and (when ``eligible_approver_roles`` is set)
      holds one of the eligible roles.
    - ``"denied_originator"`` — the actor equals
      ``case.created_by``. The reason names the originator
      (``separation_of_duties_violation: actor is the case originator``).
    - ``"denied_role"`` — the actor differs from the originator but
      holds none of the ``eligible_approver_roles``. The reason names
      the eligible roles so the operator can grant the right role.
    - ``"bypass_system"`` — the actor carries the ``system`` role; the
      SoD check is intentionally skipped (Constitution XIV — the
      deterministic core must work without a human in the loop).

    Why return ``(outcome, reason)`` instead of raising directly: the
    caller wants to stamp the same ``outcome`` / ``reason`` pair on the
    ``WorkflowTransitioned`` audit row when the transition succeeds, so
    the helper is a pure function the caller can route through one
    branch. A denied outcome still raises :class:`Forbidden` in the
    caller; this function never raises.
    """
    if _is_system(actor_roles):
        return (
            "bypass_system",
            "system actor — separation-of-duties check skipped",
        )
    if actor_id == case.created_by:
        return (
            "denied_originator",
            "actor is the case originator (separation_of_duties_violation)",
        )
    eligible = transition.eligible_approver_roles
    if eligible and not any(role in eligible for role in actor_roles):
        return (
            "denied_role",
            f"actor does not hold any eligible_approver_roles ({', '.join(eligible)})",
        )
    return (
        "allowed",
        "different actor from originator"
        + (
            f" with eligible role ({', '.join(role for role in actor_roles if role in eligible)})"
            if eligible
            else ""
        ),
    )
