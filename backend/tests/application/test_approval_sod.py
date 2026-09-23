"""Approval / separation-of-duties tests (P02-US4 / FM-017 / P11-US5).

Acceptance scenarios (AGENTS.md §4.R5):

1. **Self-approval rejected** — a transition whose ``approval_required``
   flag is set refuses the originator with a :class:`Forbidden` that
   carries the SoD reason (K4 / P11-US5). No state change, no follow-on
   tasks, no ``WorkflowTransitioned`` audit row (Constitution III).
2. **Second eligible actor succeeds** — a different identity in an
   eligible approver role executes the same transition; the case moves,
   follow-on tasks materialize, and a single ``WorkflowTransitioned``
   audit row lands.
3. **Audit reason is explainable** — the audit row's payload carries
   ``sod_outcome="allowed"``, a non-empty ``sod_reason``, the
   ``approval_required`` flag, the ``originator_id`` (case
   ``created_by``), and the ``approver_id`` (the actor that executed
   the transition). The timeline can reconstruct who approved what on
   whose behalf (P09 / Constitution V).

Naming follows AGENTS.md §4.R5: tests are sortable
(``pytest --collect-only``) and filterable (``-k``). The structure is
// given / // when / // then with Arrange-Act-Assert.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.application.authorization import (
    Action,
    Policy,
    Resource,
    authorize,
    configure_default_policy,
)
from app.application.workflow import execute_transition as transition_uc
from app.application.workflow.execute_transition import (
    ExecuteTransitionCommand,
)
from app.domain.audit import WORKFLOW_TRANSITIONED
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.errors import Forbidden
from app.domain.workflow import (
    WorkflowDefinition,
    WorkflowState,
    WorkflowTransition,
    WorkflowVersion,
)
from tests.application.workflow._execute_transition_fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryOutboxWriter,
    InMemoryTaskRepository,
    InMemoryWorkflowInstanceRepository,
    InMemoryWorkflowVersionRepository,
    StoredInstance,
)

# ---------------------------------------------------------------------------
# Constants — single tenant, three distinct identities for the SoD tests
# ---------------------------------------------------------------------------


ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
ORIGINATOR_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
APPROVER_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
UNAUTHORIZED_USER = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
SYSTEM_USER = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_default_policy() -> None:
    """Reset the policy singleton around every test."""
    from app.application import authorization as authz_mod  # noqa: PLC0415

    original = authz_mod._default_policy
    configure_default_policy(Policy())
    yield
    configure_default_policy(original)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def outbox() -> InMemoryOutboxWriter:
    return InMemoryOutboxWriter()


# ---------------------------------------------------------------------------
# Builders — shared between the three tests below
# ---------------------------------------------------------------------------


def _build_state_machine(
    *, transition_keys: tuple[tuple[str, str, str], ...]
) -> tuple[
    WorkflowDefinition,
    WorkflowVersion,
    InMemoryCaseRepository,
    InMemoryTaskRepository,
    InMemoryWorkflowInstanceRepository,
    InMemoryWorkflowVersionRepository,
    Case,
]:
    """Build a fully wired seeded environment for a single workflow.

    Returns the workflow definition, the published version, the
    repositories, and a ``Case`` whose ``created_by`` is
    :data:`ORIGINATOR_ID` so the SoD tests have a known originator to
    reuse. The case is in the ``"intake"`` state and the instance is
    pinned to the returned version.
    """
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    instance_repo = InMemoryWorkflowInstanceRepository()
    workflow_version_repo = InMemoryWorkflowVersionRepository()

    states = (
        WorkflowState(key="intake", label="Intake", is_initial=True),
        WorkflowState(key="in_review", label="In Review"),
        WorkflowState(key="decided", label="Decided"),
        WorkflowState(key="closed", label="Closed", is_terminal=True),
    )
    transitions = tuple(
        WorkflowTransition(
            key=key,
            from_state=from_state,
            to_state=to_state,
            human_driven=True,
        )
        for key, from_state, to_state in transition_keys
    )
    definition = WorkflowDefinition.create(
        tenant_id=ACME_TENANT,
        key="ServiceRequestWorkflow",
        name="Service Request Workflow",
    )
    version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=definition.id,
        version_number=1,
        states=states,
        transitions=transitions,
    )
    published = version.publish(
        actor_id=UUID("00000000-0000-0000-0000-000000000099"),
        at=FIXED_NOW,
    )

    case_type_version = CaseTypeVersion(
        id=uuid4(),
        tenant_id=ACME_TENANT,
        case_type_id=uuid4(),
        version_number=1,
        schema=CaseTypeSchema(
            fields=(CaseTypeField(key="title", label="Title", type="text", required=True),)
        ),
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=FIXED_NOW,
        published_by=ORIGINATOR_ID,
    )
    case = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference(value="C-ACME-2026-000042"),
        case_type_version=case_type_version,
        title="Service request",
        description="",
        data={"title": "Service request"},
        priority="normal",
        created_by=ORIGINATOR_ID,
        now=FIXED_NOW,
        sensitivity="internal",
    )
    case_repo.add(case)

    return definition, published, case_repo, task_repo, instance_repo, workflow_version_repo, case


def _seed_instance(
    *,
    instance_repo: InMemoryWorkflowInstanceRepository,
    definition: WorkflowDefinition,
    version: WorkflowVersion,
    case: Case,
    current_state: str = "intake",
) -> StoredInstance:
    """Insert the WorkflowInstance row that pins the case to the version."""
    instance = StoredInstance(
        id=uuid4(),
        tenant_id=ACME_TENANT,
        case_id=case.id,
        workflow_definition_id=definition.id,
        workflow_version_id=version.id,
        current_state=current_state,
        correlation_id="corr-sod",
        version_token=1,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    instance_repo.add(instance)
    return instance


def _build_approval_workflow(
    *,
    transition_keys: tuple[tuple[str, str, str], ...],
    approval_transition_key: str,
    eligible_approver_roles: tuple[str, ...] = (),
) -> tuple[
    WorkflowDefinition,
    WorkflowVersion,
    InMemoryCaseRepository,
    InMemoryTaskRepository,
    InMemoryWorkflowInstanceRepository,
    InMemoryWorkflowVersionRepository,
    Case,
]:
    """Build a workflow where ONE transition carries the approval gate.

    ``transition_keys`` enumerates the structural transitions; the
    helper upgrades the entry whose key matches
    ``approval_transition_key`` to set ``approval_required=True`` and
    ``eligible_approver_roles``. Other transitions remain plain
    ``human_driven`` moves. The helper stores BOTH the baseline v1
    and the approval-aware v2 in the repository so callers can pin
    the instance to whichever version the test exercises.

    The returned ``WorkflowVersion`` is the approval-aware v2 — the
    caller MUST pin the seeded instance to it (``_seed_instance``
    does this when given ``version=published``).
    """
    (
        definition,
        _,
        case_repo,
        task_repo,
        instance_repo,
        workflow_version_repo,
        case,
    ) = _build_state_machine(transition_keys=transition_keys)
    # Build the baseline v1 (no approval flag) and the approval-aware
    # v2 against the same definition. Both versions are stored so
    # callers that want to exercise the non-approval path can pin
    # against v1.
    baseline_v1 = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=definition.id,
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="in_review", label="In Review"),
            WorkflowState(key="decided", label="Decided"),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=tuple(
            WorkflowTransition(
                key=key,
                from_state=from_state,
                to_state=to_state,
                human_driven=True,
            )
            for key, from_state, to_state in transition_keys
        ),
    )
    workflow_version_repo._by_id[baseline_v1.id] = baseline_v1.publish(
        actor_id=UUID("00000000-0000-0000-0000-000000000099"),
        at=FIXED_NOW,
    )

    new_transitions: list[WorkflowTransition] = []
    for key, from_state, to_state in transition_keys:
        if key == approval_transition_key:
            new_transitions.append(
                WorkflowTransition(
                    key=key,
                    from_state=from_state,
                    to_state=to_state,
                    human_driven=True,
                    approval_required=True,
                    eligible_approver_roles=eligible_approver_roles,
                )
            )
        else:
            new_transitions.append(
                WorkflowTransition(
                    key=key,
                    from_state=from_state,
                    to_state=to_state,
                    human_driven=True,
                )
            )
    new_version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=definition.id,
        version_number=2,
        states=baseline_v1.states,
        transitions=tuple(new_transitions),
    )
    published = new_version.publish(
        actor_id=UUID("00000000-0000-0000-0000-000000000099"),
        at=FIXED_NOW,
    )
    workflow_version_repo._by_id[published.id] = published
    return (
        definition,
        published,
        case_repo,
        task_repo,
        instance_repo,
        workflow_version_repo,
        case,
    )


def _actor(*, user_id: UUID, roles: tuple[str, ...]) -> object:
    """Duck-typed ``UserContext`` for the policy + SoD helper.

    The stub mirrors the production ``app.deps.UserContext``: an actor
    whose roles include ``"system"`` carries ``is_system=True`` so the
    policy's system-bypass path (``AuthorizationPolicy.evaluate``
    step 2) grants ``case.transition`` to internal jobs, and the
    application layer's SoD helper (``_is_system``) skips the
    originator check.
    """

    class _Stub:
        def __init__(self) -> None:
            self.tenant_id = ACME_TENANT
            self.user_id = user_id
            self.roles = roles
            self.is_system = "system" in roles
            self.display_name = ""

    return _Stub()


# ---------------------------------------------------------------------------
# 1. Self-approval rejected — the originator cannot approve their own
#    consequential transition. K4 / P11-US5 / FM-017.
# ---------------------------------------------------------------------------


async def test_approval_sod__self_approval_rejected_with_sod_reason(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """The originator cannot execute an ``approval_required`` transition.

    The use case raises :class:`Forbidden` with structured
    ``details={"reason": "separation_of_duties_violation", ...}``
    (HTTP 403 ``forbidden`` envelope). No state change, no follow-on
    tasks, no ``WorkflowTransitioned`` row, no outbox row
    (Constitution III — no partial commit). The denial IS audited as
    one ``AccessDecisionRecorded`` row (FM-045 stream) carrying
    ``sod_outcome="denied_originator"`` so the security report can
    reconstruct the attempt without mistaking it for a transition.
    """
    # given — a workflow where ``submit_for_review`` requires approval
    (
        definition,
        version,
        case_repo,
        task_repo,
        instance_repo,
        workflow_version_repo,
        case,
    ) = _build_approval_workflow(
        transition_keys=(
            ("submit_for_review", "intake", "in_review"),
            ("record_decision", "in_review", "decided"),
            ("close", "decided", "closed"),
        ),
        approval_transition_key="submit_for_review",
        eligible_approver_roles=("reviewer",),
    )
    _seed_instance(
        instance_repo=instance_repo,
        definition=definition,
        version=version,
        case=case,
    )

    # Authorize the actor ahead of the use case so the test exercises
    # the same path as the production router.
    await authorize(
        Action.CASE_TRANSITION,
        _actor(user_id=ORIGINATOR_ID, roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=case.id),
    )

    # when — the originator executes their own consequential transition
    with pytest.raises(Forbidden) as exc_info:
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=ACME_TENANT,
                actor_id=ORIGINATOR_ID,
                actor_roles=("reviewer",),
                case_id=case.id,
                transition_key="submit_for_review",
                correlation_id="corr-self-approval",
            ),
            case_repo=case_repo,
            instance_repo=instance_repo,
            workflow_version_repo=workflow_version_repo,
            task_repo=task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    # then — the error message carries the SoD reason (Constitution V —
    # never silent, never ambiguous). The message names the
    # separation-of-duties violation so the client can render a
    # targeted message.
    assert "separation_of_duties_violation" in str(exc_info.value)
    assert "submit_for_review" in str(exc_info.value)
    assert "originator" in str(exc_info.value)

    # then — no state change: the case's workflow state is still the
    # initial unset value and the instance pointer did not move.
    stored_case = await case_repo.get(ACME_TENANT, case.id)
    assert stored_case is not None
    assert stored_case.current_workflow_state is None
    stored_instance = await instance_repo.get_for_case(ACME_TENANT, case.id)
    assert stored_instance is not None
    assert stored_instance.current_state == "intake"
    assert stored_instance.version_token == 1

    # then — no follow-on tasks, no WorkflowTransitioned row, no outbox
    # row — but exactly one AccessDecisionRecorded denial row (T4
    # acceptance: self-approval returns structured forbidden WITH audit).
    assert instance_repo.bump_calls == []
    assert outbox.records == []
    assert len(audit.events) == 1
    denial = audit.events[0]
    assert denial.event_type == "AccessDecisionRecorded"
    assert denial.payload["result"] == "denied"
    assert denial.payload["reason"] == "separation_of_duties_violation"
    assert denial.payload["sod_outcome"] == "denied_originator"
    assert denial.payload["transition_key"] == "submit_for_review"
    assert denial.payload["originator_id"] == str(ORIGINATOR_ID)
    assert denial.payload["approver_id"] == str(ORIGINATOR_ID)
    # then — the Forbidden carries structured details for the 403 envelope.
    assert exc_info.value.details["reason"] == "separation_of_duties_violation"
    assert exc_info.value.details["sod_outcome"] == "denied_originator"


# ---------------------------------------------------------------------------
# 2. Second eligible actor succeeds — a different identity in an eligible
#    role executes the same transition.
# ---------------------------------------------------------------------------


async def test_approval_sod__second_eligible_actor_succeeds(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """A different identity in an eligible approver role succeeds.

    The case moves, follow-on tasks materialize, and exactly one
    ``WorkflowTransitioned`` audit row lands. The audit row's payload
    carries the SoD outcome (``"allowed"``), the SoD reason, and the
    originator / approver ids so the timeline can reconstruct who
    approved what on whose behalf.
    """
    # given — same workflow, but the approver is a DIFFERENT identity
    # from the case originator AND holds the ``reviewer`` role.
    (
        definition,
        version,
        case_repo,
        task_repo,
        instance_repo,
        workflow_version_repo,
        case,
    ) = _build_approval_workflow(
        transition_keys=(
            ("submit_for_review", "intake", "in_review"),
            ("record_decision", "in_review", "decided"),
            ("close", "decided", "closed"),
        ),
        approval_transition_key="submit_for_review",
        eligible_approver_roles=("reviewer",),
    )
    instance = _seed_instance(
        instance_repo=instance_repo,
        definition=definition,
        version=version,
        case=case,
    )

    await authorize(
        Action.CASE_TRANSITION,
        _actor(user_id=APPROVER_ID, roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=case.id),
    )

    # when — a different, eligible actor executes the transition
    result = await transition_uc.handle(
        ExecuteTransitionCommand(
            tenant_id=ACME_TENANT,
            actor_id=APPROVER_ID,
            actor_roles=("reviewer",),
            case_id=case.id,
            transition_key="submit_for_review",
            correlation_id="corr-second-actor",
        ),
        case_repo=case_repo,
        instance_repo=instance_repo,
        workflow_version_repo=workflow_version_repo,
        task_repo=task_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # then — the case advanced and the workflow instance pointer
    # bumped exactly once.
    assert result.from_state == "intake"
    assert result.to_state == "in_review"
    assert result.transition_key == "submit_for_review"

    stored_case = await case_repo.get(ACME_TENANT, case.id)
    assert stored_case is not None
    assert stored_case.current_workflow_state == "in_review"

    stored_instance = await instance_repo.get_for_case(ACME_TENANT, case.id)
    assert stored_instance is not None
    assert stored_instance.current_state == "in_review"
    assert stored_instance.version_token == 2
    assert instance_repo.bump_calls == [(instance.id, "in_review", 1)]

    # then — no follow-on tasks were configured on this transition,
    # but one ``WorkflowTransitioned`` audit row landed.
    assert result.tasks == []
    workflow_events = [e for e in audit.events if e.event_type == WORKFLOW_TRANSITIONED]
    assert len(workflow_events) == 1


# ---------------------------------------------------------------------------
# 3. Audit reason is explainable — the ``WorkflowTransitioned`` row
#    carries enough metadata for the security report to reconstruct
#    the consequential decision (P09 / Constitution V / FM-045).
# ---------------------------------------------------------------------------


async def test_approval_sod__audit_payload_carries_sod_metadata(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """The audit row carries ``sod_outcome``, ``sod_reason``,
    ``approval_required``, ``originator_id``, and ``approver_id``.

    The timeline / security report reads these fields to reconstruct
    who approved what on whose behalf — without re-running the
    workflow definition (P09-US6 / FM-045). A security reviewer can
    answer: "did the originator approve their own consequential
    transition?" from the audit row alone.
    """
    # given — same setup as test 2; different approver.
    (
        definition,
        version,
        case_repo,
        task_repo,
        instance_repo,
        workflow_version_repo,
        case,
    ) = _build_approval_workflow(
        transition_keys=(
            ("submit_for_review", "intake", "in_review"),
            ("record_decision", "in_review", "decided"),
            ("close", "decided", "closed"),
        ),
        approval_transition_key="submit_for_review",
        eligible_approver_roles=("supervisor", "reviewer"),
    )
    _seed_instance(
        instance_repo=instance_repo,
        definition=definition,
        version=version,
        case=case,
    )

    await authorize(
        Action.CASE_TRANSITION,
        _actor(user_id=APPROVER_ID, roles=("supervisor",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=case.id),
    )

    # when — the supervisor executes the approval transition.
    await transition_uc.handle(
        ExecuteTransitionCommand(
            tenant_id=ACME_TENANT,
            actor_id=APPROVER_ID,
            actor_roles=("supervisor",),
            case_id=case.id,
            transition_key="submit_for_review",
            correlation_id="corr-audit",
        ),
        case_repo=case_repo,
        instance_repo=instance_repo,
        workflow_version_repo=workflow_version_repo,
        task_repo=task_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # then — the audit row carries the SoD metadata.
    workflow_events = [e for e in audit.events if e.event_type == WORKFLOW_TRANSITIONED]
    assert len(workflow_events) == 1
    row = workflow_events[0]
    payload = row.payload

    # sod_outcome marks the SoD check as "allowed" because the
    # approver differed from the originator and held one of the
    # eligible roles. The reason names the eligible role so the
    # security report can render the decision.
    assert payload["sod_outcome"] == "allowed"
    assert payload["sod_reason"]
    assert "different actor from originator" in payload["sod_reason"]

    # approval_required is stamped as True so a reader of the audit
    # row alone can tell whether the transition was consequential
    # (without re-reading the published workflow definition).
    assert payload["approval_required"] is True

    # The originator and approver ids are stamped so the security
    # report can answer "did the same identity do both?" without a
    # join against the case / user tables.
    assert payload["originator_id"] == str(ORIGINATOR_ID)
    assert payload["approver_id"] == str(APPROVER_ID)
    assert payload["originator_id"] != payload["approver_id"]

    # Sanity — the rest of the payload is unchanged (the transition
    # itself is still a normal ``WorkflowTransitioned`` event).
    assert payload["transition_key"] == "submit_for_review"
    assert payload["from_state"] == "intake"
    assert payload["to_state"] == "in_review"
    assert row.actor_id == APPROVER_ID
    assert row.correlation_id == "corr-audit"


# ---------------------------------------------------------------------------
# Bonus coverage — system actors bypass the SoD check (Constitution XIV
#    — the deterministic core must work without a human in the loop).
#    And a non-originator, non-eligible-role actor is denied with the
#    ``denied_role`` reason.
# ---------------------------------------------------------------------------


async def test_approval_sod__ineligible_role_actor_rejected(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """A different identity WITHOUT an eligible approver role is denied.

    The ``denied_role`` reason names the eligible roles so the operator
    knows which role to grant. The behavior is symmetric to the
    originator case: a ``Forbidden`` is raised, nothing is staged, no
    audit row lands.
    """
    (
        definition,
        version,
        case_repo,
        task_repo,
        instance_repo,
        workflow_version_repo,
        case,
    ) = _build_approval_workflow(
        transition_keys=(("submit_for_review", "intake", "in_review"),),
        approval_transition_key="submit_for_review",
        eligible_approver_roles=("supervisor",),
    )
    _seed_instance(
        instance_repo=instance_repo,
        definition=definition,
        version=version,
        case=case,
    )

    await authorize(
        Action.CASE_TRANSITION,
        _actor(user_id=APPROVER_ID, roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=case.id),
    )

    with pytest.raises(Forbidden) as exc_info:
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=ACME_TENANT,
                actor_id=APPROVER_ID,
                actor_roles=("reviewer",),
                case_id=case.id,
                transition_key="submit_for_review",
                correlation_id="corr-wrong-role",
            ),
            case_repo=case_repo,
            instance_repo=instance_repo,
            workflow_version_repo=workflow_version_repo,
            task_repo=task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    # then — the message names the eligible roles (operator can grant
    # the right role) and the SoD violation type.
    msg = str(exc_info.value)
    assert "supervisor" in msg
    assert "separation_of_duties" in msg or "eligible_approver_roles" in msg

    # then — no state change and no WorkflowTransitioned/outbox rows, but
    # the denial is audited as AccessDecisionRecorded (FM-045 stream).
    assert len(audit.events) == 1
    assert audit.events[0].event_type == "AccessDecisionRecorded"
    assert audit.events[0].payload["sod_outcome"] == "denied_role"
    assert outbox.records == []
    assert exc_info.value.details["reason"] == "separation_of_duties_violation"
    stored_case = await case_repo.get(ACME_TENANT, case.id)
    assert stored_case is not None
    assert stored_case.current_workflow_state is None


async def test_approval_sod__system_actor_bypasses_sod_check(
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """A system actor (outbox dispatcher / replay) bypasses SoD.

    The ``bypass_system`` outcome is stamped on the audit row so the
    security report can distinguish a human approval from a system
    replay (Constitution XIV — the deterministic core must work
    without a human in the loop). The system actor carries the
    ``reviewer`` role in addition to the ``system`` marker so the
    policy grants ``case.transition``; the production outbox
    dispatcher / SLA scanner all run with a role set that includes
    the ``case.transition`` grant (the ``is_system`` flag is for
    audit attribution, the role grant is for action authorization).
    """
    (
        definition,
        version,
        case_repo,
        task_repo,
        instance_repo,
        workflow_version_repo,
        case,
    ) = _build_approval_workflow(
        transition_keys=(("submit_for_review", "intake", "in_review"),),
        approval_transition_key="submit_for_review",
        eligible_approver_roles=("reviewer",),
    )
    _seed_instance(
        instance_repo=instance_repo,
        definition=definition,
        version=version,
        case=case,
    )

    await authorize(
        Action.CASE_TRANSITION,
        _actor(user_id=SYSTEM_USER, roles=("system", "reviewer")),
        Resource(tenant_id=ACME_TENANT, kind="case", id=case.id),
    )

    result = await transition_uc.handle(
        ExecuteTransitionCommand(
            tenant_id=ACME_TENANT,
            actor_id=SYSTEM_USER,
            actor_roles=("system", "reviewer"),
            case_id=case.id,
            transition_key="submit_for_review",
            correlation_id="corr-system",
        ),
        case_repo=case_repo,
        instance_repo=instance_repo,
        workflow_version_repo=workflow_version_repo,
        task_repo=task_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # then — the system actor advanced the case (Constitution XIV).
    assert result.to_state == "in_review"

    # then — the audit row stamps the bypass.
    workflow_events = [e for e in audit.events if e.event_type == WORKFLOW_TRANSITIONED]
    assert len(workflow_events) == 1
    payload = workflow_events[0].payload
    assert payload["sod_outcome"] == "bypass_system"
    assert "system" in payload["sod_reason"]
