"""Discover executable transitions and evaluate their guards (P02 / T080).

This read use case deliberately shares the transition engine's deterministic
guard context. It never mutates workflow state, writes audit rows, or exposes
case data in a guard explanation. Authorization remains server-side and is
performed before the workflow instance is inspected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.application.workflow.execute_transition import (
    GuardBlocked,
    _build_guard_context,
    _evaluate_transition_guard,
    _tasks_for_case,
)
from app.domain.errors import NotFound
from app.domain.ports import (
    CaseRepository,
    DocumentRepository,
    TaskRepository,
    WorkflowInstanceRepository,
    WorkflowVersionRepository,
)
from app.domain.workflow import WorkflowTransition, WorkflowVersionStatus


@dataclass(frozen=True, slots=True)
class PreflightTransitionsCommand:
    """Input for the tenant-scoped transition discovery operation."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TransitionPreflight:
    """Safe guard verdict for one transition available from current state.

    FM-017 fields: ``approval_required`` mirrors the transition's
    consequential flag; ``approval_eligible`` reports whether this
    actor would satisfy separation-of-duties *before* attempting;
    ``approval_reason`` carries the human-readable SoD rationale.
    """

    key: str
    from_state: str
    to_state: str
    allowed: bool
    reason: str | None = None
    details: dict[str, Any] | None = None
    approval_required: bool = False
    approval_eligible: bool = True
    approval_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PreflightTransitionsResult:
    """Current state and deterministic transition verdicts."""

    case_id: UUID
    workflow_instance_id: UUID
    workflow_version_id: UUID
    current_state: str
    transitions: tuple[TransitionPreflight, ...]


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Minimal policy identity shape, avoiding an HTTP-layer dependency."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    is_system: bool = False


async def handle(
    cmd: PreflightTransitionsCommand,
    *,
    case_repo: CaseRepository,
    instance_repo: WorkflowInstanceRepository,
    workflow_version_repo: WorkflowVersionRepository,
    task_repo: TaskRepository,
    document_repo: DocumentRepository | None = None,
) -> PreflightTransitionsResult:
    """Return outgoing transitions and guard outcomes without side effects.

    Missing or cross-tenant resources raise ``NotFound``. Actors without
    ``case.transition`` receive ``Forbidden`` from the policy. Guard failures
    are represented per transition so the client can render every option.
    """
    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")
    await authorize(
        Action.CASE_TRANSITION,
        _ActorProxy(cmd.tenant_id, cmd.actor_id, cmd.actor_roles),  # type: ignore[arg-type]
        Resource(tenant_id=cmd.tenant_id, kind="case", id=case.id),
    )
    instance = await instance_repo.get_for_case(cmd.tenant_id, cmd.case_id)
    if instance is None:
        raise NotFound(f"workflow instance not found for case {cmd.case_id}")
    version = await workflow_version_repo.get(cmd.tenant_id, instance.workflow_version_id)
    if version is None or version.status is WorkflowVersionStatus.RETIRED:
        raise NotFound(f"workflow version {instance.workflow_version_id} not found")

    tasks = await _tasks_for_case(task_repo, cmd.tenant_id, case.id)
    context = await _build_guard_context(
        case=case,
        payload=cmd.payload,
        tasks=tasks,
        document_repo=document_repo,
        tenant_id=cmd.tenant_id,
    )
    verdicts = tuple(
        _preflight_transition(
            transition=transition,
            context=context,
            document_repo_present=document_repo is not None,
            case_originator_id=case.created_by,
            actor_id=cmd.actor_id,
            actor_roles=cmd.actor_roles,
        )
        for transition in version.outgoing(instance.current_state)
    )
    return PreflightTransitionsResult(
        case_id=case.id,
        workflow_instance_id=instance.id,
        workflow_version_id=version.id,
        current_state=instance.current_state,
        transitions=verdicts,
    )


def _approval_preflight(
    *,
    transition: WorkflowTransition,
    case_originator_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
) -> tuple[bool, bool, str | None]:
    """Return ``(required, eligible, reason)`` for the SoD gate.

    Pure, no I/O: mirrors ``execute_transition._evaluate_separation_of_duties``
    so the UI can render "needs a second actor" before attempting. System
    actors (``system`` role) always satisfy the gate.
    """
    if not transition.approval_required:
        return False, True, None
    if "system" in actor_roles:
        return True, True, "system actor — separation-of-duties check skipped"
    if actor_id == case_originator_id:
        return (
            True,
            False,
            "actor is the case originator (separation_of_duties_violation)",
        )
    eligible = transition.eligible_approver_roles
    if eligible and not any(role in eligible for role in actor_roles):
        return (
            True,
            False,
            f"actor does not hold any eligible_approver_roles ({', '.join(eligible)})",
        )
    return True, True, "different actor from originator"


def _preflight_transition(
    *,
    transition: WorkflowTransition,
    context: dict[str, Any],
    document_repo_present: bool,
    case_originator_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
) -> TransitionPreflight:
    """Evaluate one transition and reduce failures to safe reason metadata."""
    approval_required, approval_eligible, approval_reason = _approval_preflight(
        transition=transition,
        case_originator_id=case_originator_id,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    if transition.guard is None:
        return TransitionPreflight(
            transition.key,
            transition.from_state,
            transition.to_state,
            True,
            approval_required=approval_required,
            approval_eligible=approval_eligible,
            approval_reason=approval_reason,
        )
    try:
        allowed = _evaluate_transition_guard(
            transition=transition,
            guard_context=context,
            transition_key=transition.key,
            document_repo_present=document_repo_present,
        )
    except GuardBlocked as exc:
        return TransitionPreflight(
            transition.key,
            transition.from_state,
            transition.to_state,
            False,
            exc.reason,
            dict(exc.details),
            approval_required=approval_required,
            approval_eligible=approval_eligible,
            approval_reason=approval_reason,
        )
    if not allowed:
        return TransitionPreflight(
            transition.key,
            transition.from_state,
            transition.to_state,
            False,
            "rule_unmet",
            approval_required=approval_required,
            approval_eligible=approval_eligible,
            approval_reason=approval_reason,
        )
    return TransitionPreflight(
        transition.key,
        transition.from_state,
        transition.to_state,
        True,
        approval_required=approval_required,
        approval_eligible=approval_eligible,
        approval_reason=approval_reason,
    )


__all__ = [
    "PreflightTransitionsCommand",
    "PreflightTransitionsResult",
    "TransitionPreflight",
    "handle",
]
