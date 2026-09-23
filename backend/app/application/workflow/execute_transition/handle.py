"""The transition orchestration use case: :func:`handle`.

Split from the flat ``execute_transition`` module (R2: files under 600
lines). ``handle`` runs the five ordered steps — authorize, resolve,
pick, guard, apply — documented in the package docstring; the
actor-proxy and transition-lookup helpers it needs live beside it
here. All action/guard/SoD helpers are imported from the sibling
submodules and re-exported through the package root.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.workflow.execute_transition._actions import (
    _apply_set_field_actions,
    _fire_start_sla_actions,
    _materialize_create_task_actions,
    _new_correlation_id,
    _tasks_for_case,
)
from app.application.workflow.execute_transition._guards import (
    _build_guard_context,
    _enforce_state_form_gate,
    _evaluate_transition_guard,
    _guard_failure_details,
    _guard_failure_reason,
)
from app.application.workflow.execute_transition._sod import (
    _evaluate_separation_of_duties,
)
from app.application.workflow.execute_transition._types import (
    SUBJECT_TYPE_CASE,
    ExecuteTransitionCommand,
    ExecuteTransitionResult,
    GuardBlocked,
    _ClockLike,
)
from app.domain.audit import ACCESS_DECISION_RECORDED, WORKFLOW_TRANSITIONED
from app.domain.errors import Forbidden, NotFound, UnsupportedTransition
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
    OutboxWriter,
    SlaClockRepository,
    TaskRepository,
    WorkflowInstanceRepository,
    WorkflowTransitionLog,
    WorkflowVersionRepository,
)
from app.domain.workflow import (
    WorkflowTransition,
    WorkflowTransitionRecord,
    WorkflowVersion,
    WorkflowVersionStatus,
)


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _actor_proxy(cmd: ExecuteTransitionCommand) -> Any:
    """Duck-typed ``UserContext`` for the policy (see create_case).

    The application layer does not import :class:`app.deps.UserContext`
    because that module pulls FastAPI in. Instead we use a dataclass
    that exposes exactly the fields the policy reads; mypy treats the
    return value as ``Any`` so it is structurally compatible with the
    ``UserContext`` parameter on :func:`authorize` without a real
    ``UserContext`` import.
    """
    return _ActorProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


def _find_transition(
    version: WorkflowVersion,
    from_state: str,
    transition_key: str,
) -> WorkflowTransition | None:
    """Return the transition matching ``from_state`` + ``key``, or None."""
    for transition in version.outgoing(from_state):
        if transition.key == transition_key:
            return transition
    return None


async def handle(
    cmd: ExecuteTransitionCommand,
    *,
    case_repo: CaseRepository,
    instance_repo: WorkflowInstanceRepository,
    workflow_version_repo: WorkflowVersionRepository,
    task_repo: TaskRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: _ClockLike,
    document_repo: DocumentRepository | None = None,
    sla_clock_repo: SlaClockRepository | None = None,
    case_type_version_repo: CaseTypeVersionRepository | None = None,
    transition_log: WorkflowTransitionLog | None = None,
    state_form_requirements: Iterable[Mapping[str, Any]] | None = None,
    submitted_form_keys: Iterable[str] | None = None,
    declared_workflow_states: Iterable[str] | None = None,
) -> ExecuteTransitionResult:
    """Execute a workflow transition atomically (FR-009 / FR-010).

    Failure behavior:

    - Auth / not-found errors are raised before any DB write.
    - A wrong transition key for the current state raises
      ``UnsupportedTransition`` (HTTP 409) — the caller asked for
      something that does not apply right now.
    - A guard failure raises ``GuardBlocked`` (HTTP 422
      ``validation_failed``) with a safe explanation that names the
      unmet requirement. No partial commit; nothing is staged.
    - The instance pointer update, the case ``current_workflow_state``
      bump, any ``set_field`` writes, the new tasks, freshly started
      SLA clocks, the audit row, and the outbox event share
      one transaction; the caller commits once. If any step raises the
      transaction rolls back so a partial transition never lands.

    Document guard evaluation (P03-US5): when a guard references
    ``case.documents_by_classification.<X>`` the use case resolves the
    per-classification count from ``DocumentRepository.list_for_case``
    and exposes it as a nested mapping in the guard context. A guard
    whose ``classification`` is missing on the case's documents raises
    ``GuardBlocked`` with ``reason="missing_documents"`` and a
    ``details`` payload that names the missing classification. The
    ``document_repo`` is optional so legacy callers that have not yet
    wired it can still run, but a document guard without the repository
    is rejected as ``guard_invalid`` (fail-closed — the runtime never
    silently accepts a guard it cannot evaluate).

    State-bound form gate (P07-US7 / FM-038): when
    ``state_form_requirements`` is supplied, every requirement of the
    form ``{"form_key", "state_key", "required"}`` that targets the
    transition's ``to_state`` with ``required=True`` must appear in
    ``submitted_form_keys`` or the transition is rejected with
    ``GuardBlocked`` (``reason="missing_required_form"``) before any
    write. When both are ``None`` the gate is skipped so legacy
    callers without form bindings behave exactly as before.
    """
    # 1. Resolve the case. Missing or cross-tenant ids surface as
    # NotFound; the two cases are indistinguishable so the API never
    # confirms cross-tenant existence (Constitution V).
    case = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")

    # 2. Authorize against the case resource. The policy raises
    # ``Forbidden`` on deny; the HTTP layer maps it to 403.
    await authorize(
        Action.CASE_TRANSITION,
        _actor_proxy(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="case", id=case.id),
    )

    # 3. Resolve the workflow instance. A case without a started
    # workflow instance is an operator error (the case-creation path
    # always starts a workflow); surface as NotFound so the API is
    # honest about the missing state.
    instance = await instance_repo.get_for_case(cmd.tenant_id, cmd.case_id)
    if instance is None:
        raise NotFound(f"workflow instance not found for case {cmd.case_id}")

    # 4. Load the pinned workflow version. The instance is bound to
    # exactly one version for its entire lifetime (P02 pillar §2 —
    # running cases remain pinned).
    version = await workflow_version_repo.get(cmd.tenant_id, instance.workflow_version_id)
    if version is None or version.status is WorkflowVersionStatus.RETIRED:
        raise NotFound(f"workflow version {instance.workflow_version_id} not found")

    # 5. Find the transition. The match is exact-key + from-state;
    # ambiguity (two transitions out of the same state with the same
    # key) is rejected at publish time, so the lookup is unambiguous.
    from_state = instance.current_state
    transition = _find_transition(version, from_state, cmd.transition_key)
    if transition is None:
        raise UnsupportedTransition(
            f"transition {cmd.transition_key!r} is not allowed from state {from_state!r}"
        )

    # 6. Evaluate the guard. A guard that does not exist is treated
    # as ``True`` (the publish-time validator insists every transition
    # declares an explicit guard when one is intended; an empty guard
    # means "always allowed").
    guard_context = await _build_guard_context(
        case=case,
        payload=cmd.payload,
        tasks=await _tasks_for_case(task_repo, cmd.tenant_id, case.id),
        document_repo=document_repo,
        tenant_id=cmd.tenant_id,
    )
    if transition.guard is not None:
        # Document-requirements guards evaluate inline so the response
        # surfaces ``reason="missing_documents"`` (P03-US5); every other
        # guard is handed to ``domain.rules.evaluate``. A document guard
        # without a document repository fails closed — the runtime
        # never silently accepts a guard it cannot evaluate.
        passed = _evaluate_transition_guard(
            transition=transition,
            guard_context=guard_context,
            transition_key=cmd.transition_key,
            document_repo_present=document_repo is not None,
        )
        if not passed:
            raise GuardBlocked(
                f"transition {cmd.transition_key!r} guard not met",
                reason=_guard_failure_reason(transition.guard),
                details={
                    "transition_key": cmd.transition_key,
                    **_guard_failure_details(transition.guard),
                },
            )
    # 6b. State-bound form gate (P07-US7 / FM-038). A transition that
    # lands in a state with required state-bound forms commits only
    # with those forms submitted. The check is pure and deterministic
    # (no DB access; missing keys sorted) and runs before any write
    # so a rejected transition leaves no partial state behind.
    if state_form_requirements is not None:
        _enforce_state_form_gate(
            transition=transition,
            requirements=state_form_requirements,
            submitted_form_keys=submitted_form_keys,
            declared_states=declared_workflow_states,
        )
    # 7. Approval / separation-of-duties gate (P02-US4 / FM-017 /
    # P11-US5). When the transition declares ``approval_required`` the
    # engine refuses self-approval (originator cannot approve their own
    # consequential decision). The check runs after the guard so a
    # transition that fails for a structural reason still produces a
    # single 422 ``guard_invalid`` instead of a misleading 403. The
    # ``eligible_approver_roles`` list is the (optional) role gate; an
    # empty tuple means "any role with ``case.transition`` who is not
    # the originator". ``is_system`` actors bypass the SoD check so the
    # outbox dispatcher / SLA scanner can replay transitions on behalf
    # of the platform without a human grant (Constitution XIV — the
    # deterministic core must work without a human in the loop).
    # We ALWAYS evaluate SoD when approval_required so the audit row
    # gets the correct sod_outcome ("allowed", "denied_originator",
    # "denied_role", "bypass_system", "not_required").
    sod_outcome = "not_required"
    sod_reason = ""
    if transition.approval_required:
        sod_outcome, sod_reason = _evaluate_separation_of_duties(
            transition=transition,
            case=case,
            actor_id=cmd.actor_id,
            actor_roles=cmd.actor_roles,
        )
        if sod_outcome not in ("allowed", "bypass_system"):
            # FR-009 / P11-US5 / K4 / FM-017 T4 acceptance: a
            # self-approval attempt is a 403 ``forbidden`` with a
            # structured reason of ``separation_of_duties_violation``
            # AND an explainable audit row. The denial lands as an
            # ``AccessDecisionRecorded`` event (FM-045 stream — never a
            # ``WorkflowTransitioned`` row, so the case timeline cannot
            # mistake a denial for a successful transition). The HTTP
            # layer maps ``Forbidden`` to
            # ``{"code": "forbidden", "details": {"reason": ...}}``;
            # the SoD reason travels both in the message and in
            # ``details`` so the client renders "needs a second actor"
            # without re-reading the workflow definition.
            now_denied = clock.now()
            await audit.append(
                tenant_id=cmd.tenant_id,
                event_type=ACCESS_DECISION_RECORDED,
                actor_id=cmd.actor_id,
                actor_type="user",
                subject_type=SUBJECT_TYPE_CASE,
                subject_id=case.id,
                case_id=case.id,
                correlation_id=cmd.correlation_id or _new_correlation_id(),
                causation_id=cmd.causation_id,
                payload={
                    "action": "case.transition",
                    "result": "denied",
                    "reason": "separation_of_duties_violation",
                    "sod_outcome": sod_outcome,
                    "sod_reason": sod_reason,
                    "transition_key": cmd.transition_key,
                    "from_state": from_state,
                    "approval_required": True,
                    "originator_id": str(case.created_by),
                    "approver_id": str(cmd.actor_id),
                },
                occurred_at=now_denied,
            )
            raise Forbidden(
                f"transition {cmd.transition_key!r} requires a different "
                f"actor from the case originator: {sod_reason}",
                details={
                    "reason": "separation_of_duties_violation",
                    "sod_outcome": sod_outcome,
                    "transition_key": cmd.transition_key,
                },
            )

    # 8. Apply the transition atomically.
    now = clock.now()
    to_state = transition.to_state

    new_instance = await instance_repo.bump_state(
        tenant_id=cmd.tenant_id,
        instance_id=instance.id,
        new_state=to_state,
        expected_version_token=instance.version_token,
        occurred_at=now,
    )

    new_case = case.set_workflow_state(state=to_state, now=now)

    # `set_field` actions write case fields before the single UPDATE
    # below so state bump + field writes share one compare-and-swap.
    new_case, fields_written = await _apply_set_field_actions(
        new_case,
        transition.actions,
        tenant_id=cmd.tenant_id,
        case_type_version_repo=case_type_version_repo,
        now=now,
    )
    await case_repo.update(new_case)

    # Materialize `create_task` actions. The helper deduplicates
    # against task types already open on the case (welcome vs
    # transition double-fire — ADR-029) and persists each new task.
    new_tasks = await _materialize_create_task_actions(
        task_repo=task_repo,
        tenant_id=cmd.tenant_id,
        case_id=case.id,
        actions=transition.actions,
        transition_key=cmd.transition_key,
        now=now,
    )

    sla_started = await _fire_start_sla_actions(
        tenant_id=cmd.tenant_id,
        case_id=case.id,
        actions=transition.actions,
        sla_clock_repo=sla_clock_repo,
        now=now,
    )

    correlation_id = cmd.correlation_id or _new_correlation_id()

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=WORKFLOW_TRANSITIONED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_CASE,
        subject_id=new_case.id,
        case_id=new_case.id,
        correlation_id=correlation_id,
        causation_id=cmd.causation_id,
        payload={
            "transition_key": cmd.transition_key,
            "from_state": from_state,
            "to_state": to_state,
            "workflow_version_id": str(version.id),
            "human_driven": transition.human_driven,
            "task_count": len(new_tasks),
            "fields_written": fields_written,
            "sla_started": sla_started,
            "transitioned_at": now.isoformat(),
            # P02-US4 / FM-017 / P11-US5: stamp the SoD outcome on the
            # audit row so the timeline carries an explainable record
            # of every consequential decision. ``sod_outcome`` is
            # ``"allowed"`` when the transition required approval and a
            # different eligible actor executed it, ``"not_required"``
            # when the transition was not consequential, or
            # ``"bypass_system"`` when a system actor replayed it. The
            # ``sod_reason`` carries the human-readable rationale
            # ("originator cannot self-approve", "eligible approver
            # roles: supervisor") that the security report uses to
            # reconstruct the decision (P09-US6 / FM-045).
            "sod_outcome": sod_outcome,
            "sod_reason": sod_reason,
            "approval_required": transition.approval_required,
            "originator_id": str(case.created_by),
            "approver_id": str(cmd.actor_id),
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=WORKFLOW_TRANSITIONED,
        event_id=uuid4(),
        payload={
            "workflow_instance_id": str(instance.id),
            "case_id": str(new_case.id),
            "tenant_id": str(cmd.tenant_id),
            "actor_id": str(cmd.actor_id),
            "transition_key": cmd.transition_key,
            "from_state": from_state,
            "to_state": to_state,
            "workflow_version": version.version_number,
            "occurred_at": now.isoformat(),
        },
    )

    # Append-only transition log (T069). Staged on the same
    # transaction so the log row and the audit/outbox rows commit
    # together; without a log the table simply stays empty.
    await _record_transition(
        transition_log,
        instance_id=new_instance.id,
        tenant_id=cmd.tenant_id,
        from_state=from_state,
        to_state=to_state,
        transition_key=cmd.transition_key,
        actor_id=cmd.actor_id,
        occurred_at=now,
        payload={
            "task_count": len(new_tasks),
            "fields_written": fields_written,
            "sla_started": sla_started,
        },
        correlation_id=correlation_id,
        causation_id=cmd.causation_id,
    )

    return ExecuteTransitionResult(
        case=new_case,
        instance_id=new_instance.id,
        from_state=from_state,
        to_state=to_state,
        transition_key=cmd.transition_key,
        tasks=new_tasks,
        transitioned_at=now,
    )


# ----- helpers --------------------------------------------------------------


async def _record_transition(
    transition_log: WorkflowTransitionLog | None,
    *,
    instance_id: UUID,
    tenant_id: UUID,
    from_state: str,
    to_state: str,
    transition_key: str,
    actor_id: UUID,
    occurred_at: datetime,
    payload: dict[str, Any],
    correlation_id: str,
    causation_id: str | None,
) -> None:
    """Stage the append-only transition-log row (T069).

    A ``None`` log means the caller runs without transition history
    (unit tests); the table simply stays empty.
    """
    if transition_log is None:
        return
    await transition_log.record(
        WorkflowTransitionRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            workflow_instance_id=instance_id,
            from_state=from_state,
            to_state=to_state,
            transition_key=transition_key,
            actor_id=actor_id,
            occurred_at=occurred_at,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
    )
