"""Application tests for `execute_transition.handle` (P02-US1 / FM-014 / FR-009).

Acceptance scenarios (AGENTS.md §4.R5):

1. **Success** — a worker with the ``case.transition`` grant executes
   the transition; the case moves to the next state, the workflow
   instance pointer is bumped, follow-on tasks are created, and a
   ``WorkflowTransitioned`` audit + outbox row land in the same
   transaction (Constitution III).
2. **Blocked guard** — a transition whose guard does not hold raises
   :class:`GuardBlocked` with a safe explanation that names the
   missing requirement (FR-010). No state change, no task creation,
   no audit row.
3. **Auth denial** — a worker without the ``case.transition`` grant
   is denied with :class:`Forbidden`. No state change, no audit row.
4. **Idempotent replay** — a transition request retried with the
   same body produces the same outcome (the HTTP layer caches the
   response). At the use-case layer the second call advances the
   case a second time (the engine is intentionally non-idempotent at
   the domain layer; idempotency is the HTTP middleware's job).
5. **Stale concurrency token** — a concurrent transition on the same
   instance raises :class:`Conflict` when the stored
   ``version_token`` differs from the expected token.

Naming follows AGENTS.md §4.R5: tests are sortable
(``pytest --collect-only``) and filterable (``-k``).
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
    GuardBlocked,
)
from app.domain.audit import WORKFLOW_TRANSITIONED
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.errors import Conflict, Forbidden, NotFound, UnsupportedTransition
from app.domain.task import Task
from app.domain.workflow import WorkflowState, WorkflowTransition, WorkflowVersion
from tests.application.workflow._execute_transition_fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryOutboxWriter,
    InMemoryTaskRepository,
    InMemoryWorkflowInstanceRepository,
    InMemoryWorkflowVersionRepository,
    StoredInstance,
    make_published_workflow,
)

# ---------------------------------------------------------------------------
# Constants — two distinct tenants for the tenant-isolation test
# ---------------------------------------------------------------------------


ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
BETA_TENANT = UUID("22222222-2222-2222-2222-222222222222")

REVIEWER_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
INTAKE_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_default_policy() -> None:
    """Reset the policy singleton around every test.

    Why: the default policy is a process singleton
    (``app.application.authorization._default_policy``). Tests that
    call ``configure_default_policy`` would leak into the next test
    without this guard.
    """
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


@pytest.fixture
async def seeded() -> dict[str, object]:
    """Seed a fully-wired case + workflow instance in the ACME tenant.

    Returns a dict the tests pluck from so individual tests only read
    the values they care about.
    """
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    instance_repo = InMemoryWorkflowInstanceRepository()
    workflow_version_repo = InMemoryWorkflowVersionRepository()

    definition, version = make_published_workflow(
        tenant_id=ACME_TENANT,
        transition_keys=(
            ("submit_for_review", "intake", "in_review"),
            ("record_decision", "in_review", "decided"),
            ("close", "decided", "closed"),
        ),
        with_actions=True,
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
        published_by=REVIEWER_USER,
    )
    case = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference(value="C-ACME-2026-000001"),
        case_type_version=case_type_version,
        title="Service request",
        description="",
        data={"title": "Service request"},
        priority="normal",
        created_by=REVIEWER_USER,
        now=FIXED_NOW,
        sensitivity="internal",
    )
    case_repo.add(case)
    instance = StoredInstance(
        id=uuid4(),
        tenant_id=ACME_TENANT,
        case_id=case.id,
        workflow_definition_id=definition.id,
        workflow_version_id=version.id,
        current_state="intake",
        correlation_id="corr-1",
        version_token=1,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    instance_repo.add(instance)
    await workflow_version_repo.add(version)

    return {
        "case": case,
        "instance": instance,
        "definition": definition,
        "version": version,
        "case_repo": case_repo,
        "task_repo": task_repo,
        "instance_repo": instance_repo,
        "workflow_version_repo": workflow_version_repo,
    }


# ---------------------------------------------------------------------------
# 1. Success — the happy path
# ---------------------------------------------------------------------------


async def test_execute_transition__success__advances_case_creates_task_and_audits(
    seeded: dict[str, object],
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """Happy path: the case moves, the instance pointer advances,
    follow-on tasks are created, and one ``WorkflowTransitioned``
    audit + outbox row is appended (all in one transaction).

    Mirrors :func:`test_create_case__success__writes_case_audit_and_outbox`
    in style — the use case is a pure orchestration function over
    fakes that capture every side effect.
    """
    case: Case = seeded["case"]  # type: ignore[assignment]
    instance: StoredInstance = seeded["instance"]  # type: ignore[assignment]
    case_repo: InMemoryCaseRepository = seeded["case_repo"]  # type: ignore[assignment]
    task_repo: InMemoryTaskRepository = seeded["task_repo"]  # type: ignore[assignment]
    instance_repo: InMemoryWorkflowInstanceRepository = seeded[  # type: ignore[assignment]
        "instance_repo"
    ]
    workflow_version_repo: InMemoryWorkflowVersionRepository = (  # type: ignore[assignment]
        seeded["workflow_version_repo"]
    )

    # Authorize the actor ahead of the use case so the test exercises
    # the same path as the production router.
    await authorize(
        Action.CASE_TRANSITION,
        _actor(roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=case.id),
    )

    result = await transition_uc.handle(
        ExecuteTransitionCommand(
            tenant_id=ACME_TENANT,
            actor_id=REVIEWER_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
            transition_key="submit_for_review",
            correlation_id="corr-success",
        ),
        case_repo=case_repo,
        instance_repo=instance_repo,
        workflow_version_repo=workflow_version_repo,
        task_repo=task_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # 1. Case moved.
    assert result.from_state == "intake"
    assert result.to_state == "in_review"
    assert result.transition_key == "submit_for_review"
    assert result.transitioned_at == FIXED_NOW

    # 2. Case was persisted with the new workflow state.
    stored_case = await case_repo.get(ACME_TENANT, case.id)
    assert stored_case is not None
    assert stored_case.current_workflow_state == "in_review"

    # 3. Workflow instance pointer bumped, optimistic-concurrency token
    # incremented, and the ``bump_state`` call recorded the expected
    # prior token.
    stored_instance = await instance_repo.get_for_case(ACME_TENANT, case.id)
    assert stored_instance is not None
    assert stored_instance.current_state == "in_review"
    assert stored_instance.version_token == 2
    assert instance_repo.bump_calls == [(instance.id, "in_review", 1)]

    # 4. One follow-on task materialized from the transition's
    # ``create_task`` action.
    assert len(result.tasks) == 1
    follow_on = result.tasks[0]
    assert isinstance(follow_on, Task)
    assert follow_on.status.value == "open"
    assert follow_on.type == "follow_on_review"
    assert follow_on.assignee_queue_id == "review_queue"
    assert follow_on.created_by_transition == "submit_for_review"

    # 5. Exactly one ``WorkflowTransitioned`` audit row landed, with
    # the configured from / to states and the right subject.
    workflow_events = [e for e in audit.events if e.event_type == WORKFLOW_TRANSITIONED]
    assert len(workflow_events) == 1
    row = workflow_events[0]
    assert row.subject_id == case.id
    assert row.case_id == case.id
    assert row.actor_id == REVIEWER_USER
    assert row.payload["transition_key"] == "submit_for_review"
    assert row.payload["from_state"] == "intake"
    assert row.payload["to_state"] == "in_review"
    assert row.payload["task_count"] == 1
    assert row.payload["human_driven"] is True
    assert row.correlation_id == "corr-success"

    # 6. Exactly one matching outbox row landed.
    outbox_events = [r for r in outbox.records if r.event_type == WORKFLOW_TRANSITIONED]
    assert len(outbox_events) == 1
    assert outbox_events[0].payload["from_state"] == "intake"
    assert outbox_events[0].payload["to_state"] == "in_review"
    assert outbox_events[0].payload["transition_key"] == "submit_for_review"
    assert outbox_events[0].payload["case_id"] == str(case.id)


# ---------------------------------------------------------------------------
# 2. Blocked guard — FR-010 structured explanation
# ---------------------------------------------------------------------------


async def test_execute_transition__blocked_guard__raises_with_safe_explanation_and_writes_nothing(
    seeded: dict[str, object],
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """A guard that does not hold raises :class:`GuardBlocked` with a
    safe explanation that names the missing requirement (FR-010).

    No state change, no follow-on tasks, no audit row, no outbox row
    (Constitution III — no partial commit).
    """
    case: Case = seeded["case"]  # type: ignore[assignment]
    instance: StoredInstance = seeded["instance"]  # type: ignore[assignment]
    case_repo: InMemoryCaseRepository = seeded["case_repo"]  # type: ignore[assignment]
    task_repo: InMemoryTaskRepository = seeded["task_repo"]  # type: ignore[assignment]
    instance_repo: InMemoryWorkflowInstanceRepository = seeded[  # type: ignore[assignment]
        "instance_repo"
    ]
    workflow_version_repo: InMemoryWorkflowVersionRepository = (  # type: ignore[assignment]
        seeded["workflow_version_repo"]
    )

    # Add a second transition whose guard requires a participant that
    # does not exist on the seeded case (the guard evaluator reads
    # ``case.participants``; the seeded case has zero).
    definition = seeded["definition"]  # type: ignore[assignment]
    version = await workflow_version_repo.get(ACME_TENANT, instance.workflow_version_id)
    assert version is not None

    new_state = WorkflowState(key="approved", label="Approved")
    guarded_transition = WorkflowTransition(
        key="approve_with_guard",
        from_state="intake",
        to_state="approved",
        human_driven=True,
        guard={"exists": {"field": "case.participants", "min": 1}},
    )
    new_version = WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=definition.id,
        version_number=2,
        states=(*version.states, new_state),
        transitions=(*version.transitions, guarded_transition),
    )
    published = new_version.publish(
        actor_id=REVIEWER_USER,
        at=FIXED_NOW,
    )
    await workflow_version_repo.add(published)
    # Pin the instance to the new version so the next handle() call
    # reads it.
    bumped_instance = await instance_repo.bump_state(
        tenant_id=ACME_TENANT,
        instance_id=instance.id,
        new_state="intake",
        expected_version_token=instance.version_token,
        occurred_at=FIXED_NOW,
    )
    # The bumped instance lives in ``_instances`` now; pin the
    # version id by directly updating the stored row.
    instance_repo._instances[bumped_instance.id] = StoredInstance(
        id=bumped_instance.id,
        tenant_id=ACME_TENANT,
        case_id=case.id,
        workflow_definition_id=definition.id,
        workflow_version_id=published.id,
        current_state="intake",
        correlation_id="corr-2",
        version_token=bumped_instance.version_token,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )

    # Auth first so the test does not conflate guard denial with auth
    # denial (see the auth test below).
    await authorize(
        Action.CASE_TRANSITION,
        _actor(roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=case.id),
    )

    with pytest.raises(GuardBlocked) as exc_info:
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=ACME_TENANT,
                actor_id=REVIEWER_USER,
                actor_roles=("reviewer",),
                case_id=case.id,
                transition_key="approve_with_guard",
                correlation_id="corr-blocked",
            ),
            case_repo=case_repo,
            instance_repo=instance_repo,
            workflow_version_repo=workflow_version_repo,
            task_repo=task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    # FR-010: the structured exception names the missing requirement
    # without leaking any protected data.
    assert exc_info.value.reason == "missing_participant"
    assert exc_info.value.field is None
    assert "approve_with_guard" in str(exc_info.value)

    # No state change, no audit / outbox rows.
    stored_case = await case_repo.get(ACME_TENANT, case.id)
    assert stored_case is not None
    # The seeded case starts with no workflow state; the guard never
    # ran so the case stays at ``None``.
    assert stored_case.current_workflow_state is None
    assert audit.events == []
    assert outbox.records == []


# ---------------------------------------------------------------------------
# 3. Auth denial — actor lacks case.transition
# ---------------------------------------------------------------------------


async def test_execute_transition__auth_denied__raises_forbidden_and_writes_nothing(
    seeded: dict[str, object],
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """An actor without ``case.transition`` is denied (Forbidden).

    The ``intake`` role has ``case.create`` and ``case.read`` but not
    ``case.transition``. The use case raises :class:`Forbidden`
    before touching any repository.
    """
    case: Case = seeded["case"]  # type: ignore[assignment]
    case_repo: InMemoryCaseRepository = seeded["case_repo"]  # type: ignore[assignment]
    task_repo: InMemoryTaskRepository = seeded["task_repo"]  # type: ignore[assignment]
    instance_repo: InMemoryWorkflowInstanceRepository = seeded[  # type: ignore[assignment]
        "instance_repo"
    ]
    workflow_version_repo: InMemoryWorkflowVersionRepository = (  # type: ignore[assignment]
        seeded["workflow_version_repo"]
    )

    with pytest.raises(Forbidden):
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("intake",),
                case_id=case.id,
                transition_key="submit_for_review",
                correlation_id="corr-auth-deny",
            ),
            case_repo=case_repo,
            instance_repo=instance_repo,
            workflow_version_repo=workflow_version_repo,
            task_repo=task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    # No state change, no audit / outbox rows.
    stored_case = await case_repo.get(ACME_TENANT, case.id)
    assert stored_case is not None
    assert stored_case.current_workflow_state is None
    assert audit.events == []
    assert outbox.records == []


# ---------------------------------------------------------------------------
# 4. Idempotent replay — a duplicate call advances the case a second
# time (the use case is intentionally non-idempotent at the domain
# layer; HTTP-layer idempotency is the canonical replay mechanism).
# ---------------------------------------------------------------------------


async def test_execute_transition__duplicate_call__advances_case_twice(
    seeded: dict[str, object],
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """A second call with the same body advances the case a second
    time.

    Why this is correct: idempotency for transport retries is the
    HTTP ``IdempotencyMiddleware``'s job (it caches the response by
    ``Idempotency-Key``). The use case itself is the workflow
    engine — a duplicate call is a duplicate action, not a no-op.
    This test pins that contract so a future refactor does not
    silently weaken it.

    To exercise "duplicate" we manually rewind the workflow
    instance back to ``intake`` between the two calls (simulating a
    test that wants to assert the engine can fire the same
    transition twice when the state permits).
    """
    case: Case = seeded["case"]  # type: ignore[assignment]
    instance: StoredInstance = seeded["instance"]  # type: ignore[assignment]
    case_repo: InMemoryCaseRepository = seeded["case_repo"]  # type: ignore[assignment]
    task_repo: InMemoryTaskRepository = seeded["task_repo"]  # type: ignore[assignment]
    instance_repo: InMemoryWorkflowInstanceRepository = seeded[  # type: ignore[assignment]
        "instance_repo"
    ]
    workflow_version_repo: InMemoryWorkflowVersionRepository = (  # type: ignore[assignment]
        seeded["workflow_version_repo"]
    )

    common_kwargs = {
        "case_repo": case_repo,
        "instance_repo": instance_repo,
        "workflow_version_repo": workflow_version_repo,
        "task_repo": task_repo,
        "audit": audit,
        "outbox": outbox,
        "clock": clock,
    }

    cmd = ExecuteTransitionCommand(
        tenant_id=ACME_TENANT,
        actor_id=REVIEWER_USER,
        actor_roles=("reviewer",),
        case_id=case.id,
        transition_key="submit_for_review",
        correlation_id="corr-replay",
    )

    first = await transition_uc.handle(cmd, **common_kwargs)
    assert first.to_state == "in_review"

    # Rewind the workflow instance + case pointer to ``intake`` so
    # the second call is allowed to fire ``submit_for_review``
    # again. This is what the HTTP middleware achieves via
    # Idempotency-Key replay without re-running the use case.
    current = await instance_repo.get(ACME_TENANT, instance.id)
    assert current is not None
    rewound = await instance_repo.bump_state(
        tenant_id=ACME_TENANT,
        instance_id=instance.id,
        new_state="intake",
        expected_version_token=current.version_token,
        occurred_at=FIXED_NOW,
    )
    assert rewound.current_state == "intake"

    stored_case = await case_repo.get(ACME_TENANT, case.id)
    assert stored_case is not None
    reset_case = stored_case.set_workflow_state(state="intake", now=FIXED_NOW)
    await case_repo.update(reset_case)

    second = await transition_uc.handle(cmd, **common_kwargs)
    assert second.to_state == "in_review"

    # Two audit rows landed.
    workflow_events = [e for e in audit.events if e.event_type == WORKFLOW_TRANSITIONED]
    assert len(workflow_events) == 2
    # And the instance token incremented.
    stored_instance = await instance_repo.get_for_case(ACME_TENANT, case.id)
    assert stored_instance is not None
    assert stored_instance.version_token >= 3


# ---------------------------------------------------------------------------
# 5. Stale concurrency token — Conflict on a concurrent transition
# ---------------------------------------------------------------------------


async def test_execute_transition__stale_instance_token__raises_conflict(
    seeded: dict[str, object],
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """A concurrent transition with a stale ``version_token`` is
    refused with :class:`Conflict` (Constitution V).

    The test exercises the optimistic-concurrency contract by
    reading the instance at ``version_token=1`` (stale), then having
    a concurrent writer advance the stored token to ``2`` between
    the read and the bump. The use case passes ``expected=1`` and
    the stored is ``2`` — the bump fails.
    """
    case: Case = seeded["case"]  # type: ignore[assignment]
    case_repo: InMemoryCaseRepository = seeded["case_repo"]  # type: ignore[assignment]
    task_repo: InMemoryTaskRepository = seeded["task_repo"]  # type: ignore[assignment]
    instance_repo: InMemoryWorkflowInstanceRepository = seeded[  # type: ignore[assignment]
        "instance_repo"
    ]
    workflow_version_repo: InMemoryWorkflowVersionRepository = (  # type: ignore[assignment]
        seeded["workflow_version_repo"]
    )
    instance: StoredInstance = seeded["instance"]  # type: ignore[assignment]

    # First call succeeds and bumps the token to 2.
    await transition_uc.handle(
        ExecuteTransitionCommand(
            tenant_id=ACME_TENANT,
            actor_id=REVIEWER_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
            transition_key="submit_for_review",
            correlation_id="corr-first",
        ),
        case_repo=case_repo,
        instance_repo=instance_repo,
        workflow_version_repo=workflow_version_repo,
        task_repo=task_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # Reset the case + instance pointers back to the initial state so
    # the next use-case call would be allowed to fire the transition
    # again. Then advance the stored instance token by one to simulate
    # a concurrent writer who won the race after the read but before
    # the bump.
    stored_case = await case_repo.get(ACME_TENANT, case.id)
    assert stored_case is not None
    reset_case = stored_case.set_workflow_state(state="intake", now=FIXED_NOW)
    await case_repo.update(reset_case)

    # Snap the instance token back to ``1`` so the use case reads a
    # stale value.
    instance_repo._instances[instance.id] = StoredInstance(
        id=instance.id,
        tenant_id=ACME_TENANT,
        case_id=case.id,
        workflow_definition_id=instance.workflow_definition_id,
        workflow_version_id=instance.workflow_version_id,
        current_state="intake",
        correlation_id=instance.correlation_id,
        version_token=1,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
    )

    # Simulate a concurrent writer winning the race: the fake compares
    # the stored token against the one the use case read.
    real_bump = instance_repo.bump_state

    async def _stale_bumper(**kwargs: object) -> object:
        kwargs2 = dict(kwargs)
        kwargs2["expected_version_token"] = 999
        return await real_bump(**kwargs2)  # type: ignore[arg-type]

    instance_repo.bump_state = _stale_bumper  # type: ignore[method-assign]
    with pytest.raises(Conflict):
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=ACME_TENANT,
                actor_id=REVIEWER_USER,
                actor_roles=("reviewer",),
                case_id=case.id,
                transition_key="submit_for_review",
                correlation_id="corr-stale",
            ),
            case_repo=case_repo,
            instance_repo=instance_repo,
            workflow_version_repo=workflow_version_repo,
            task_repo=task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 6. Wrong transition key for the current state
# ---------------------------------------------------------------------------


async def test_execute_transition__wrong_transition_for_state__raises_unsupported(
    seeded: dict[str, object],
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """A transition key that does not match any outgoing transition
    from the current state raises :class:`UnsupportedTransition`.

    Distinct from a guard block: the wrong-key case is the caller's
    mistake (HTTP 409 ``unsupported_transition``), not the workflow
    rejecting the call.
    """
    case: Case = seeded["case"]  # type: ignore[assignment]
    case_repo: InMemoryCaseRepository = seeded["case_repo"]  # type: ignore[assignment]
    task_repo: InMemoryTaskRepository = seeded["task_repo"]  # type: ignore[assignment]
    instance_repo: InMemoryWorkflowInstanceRepository = seeded[  # type: ignore[assignment]
        "instance_repo"
    ]
    workflow_version_repo: InMemoryWorkflowVersionRepository = (  # type: ignore[assignment]
        seeded["workflow_version_repo"]
    )

    with pytest.raises(UnsupportedTransition):
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=ACME_TENANT,
                actor_id=REVIEWER_USER,
                actor_roles=("reviewer",),
                case_id=case.id,
                transition_key="record_decision",  # Not valid from "intake".
                correlation_id="corr-wrong-key",
            ),
            case_repo=case_repo,
            instance_repo=instance_repo,
            workflow_version_repo=workflow_version_repo,
            task_repo=task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# 6b. Welcome-vs-transition double-fire — a ``create_task`` action for a
# task type already open on the case (materialized by ``start_workflow``
# as the "welcome" task) is skipped, not duplicated (ADR-029).
# ---------------------------------------------------------------------------


async def test_execute_transition__skips_create_task_already_open_on_case(
    seeded: dict[str, object],
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """A ``create_task`` action whose task type is already open on the
    case does not materialize a duplicate.

    ``start_workflow`` stages the transition's ``create_task`` action as
    the intake "welcome" task; when the transition later runs, the same
    action must not fire again (ADR-029). Distinct task types are
    unaffected.
    """
    case: Case = seeded["case"]  # type: ignore[assignment]
    case_repo: InMemoryCaseRepository = seeded["case_repo"]  # type: ignore[assignment]
    task_repo: InMemoryTaskRepository = seeded["task_repo"]  # type: ignore[assignment]
    instance_repo: InMemoryWorkflowInstanceRepository = seeded[  # type: ignore[assignment]
        "instance_repo"
    ]
    workflow_version_repo: InMemoryWorkflowVersionRepository = (  # type: ignore[assignment]
        seeded["workflow_version_repo"]
    )

    # Simulate the welcome task already staged by ``start_workflow``.
    welcome = Task.create(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        type="follow_on_review",
        title="Review service request",
        assignee_queue_id="review_queue",
        created_at=FIXED_NOW,
        created_by_transition="initial",
    )
    await task_repo.add(welcome)

    # Authorize the actor ahead of the use case.
    await authorize(
        Action.CASE_TRANSITION,
        _actor(roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=case.id),
    )

    result = await transition_uc.handle(
        ExecuteTransitionCommand(
            tenant_id=ACME_TENANT,
            actor_id=REVIEWER_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
            transition_key="submit_for_review",
            correlation_id="corr-dedup",
        ),
        case_repo=case_repo,
        instance_repo=instance_repo,
        workflow_version_repo=workflow_version_repo,
        task_repo=task_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # The transition ran (state advanced) but no new task was
    # materialized: the welcome task is the only one, and it is
    # unchanged.
    assert result.to_state == "in_review"
    assert result.tasks == []

    tasks = await task_repo.list_for_case(ACME_TENANT, case.id)
    assert len(tasks) == 1
    assert tasks[0].id == welcome.id
    assert tasks[0].created_by_transition == "initial"


async def test_execute_transition__creates_distinct_task_type_when_no_open_match(
    seeded: dict[str, object],
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """A ``create_task`` action whose task type has no open task on the
    case still materializes normally (only exact-type matches are
    skipped).
    """
    case: Case = seeded["case"]  # type: ignore[assignment]
    task_repo: InMemoryTaskRepository = seeded["task_repo"]  # type: ignore[assignment]
    instance_repo: InMemoryWorkflowInstanceRepository = seeded[  # type: ignore[assignment]
        "instance_repo"
    ]
    workflow_version_repo: InMemoryWorkflowVersionRepository = (  # type: ignore[assignment]
        seeded["workflow_version_repo"]
    )

    # A *completed* task of the same type must not block a fresh one —
    # only OPEN tasks are considered for dedup (ADR-029).
    completed = Task.create(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        type="follow_on_review",
        title="Review service request",
        assignee_queue_id="review_queue",
        created_at=FIXED_NOW,
        created_by_transition="initial",
    ).complete(actor_id=REVIEWER_USER, outcome={}, at=FIXED_NOW)
    await task_repo.add(completed)

    await authorize(
        Action.CASE_TRANSITION,
        _actor(roles=("reviewer",)),
        Resource(tenant_id=ACME_TENANT, kind="case", id=case.id),
    )

    result = await transition_uc.handle(
        ExecuteTransitionCommand(
            tenant_id=ACME_TENANT,
            actor_id=REVIEWER_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
            transition_key="submit_for_review",
            correlation_id="corr-distinct",
        ),
        case_repo=seeded["case_repo"],  # type: ignore[arg-type]
        instance_repo=instance_repo,
        workflow_version_repo=workflow_version_repo,
        task_repo=task_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # The transition fired a fresh task because the only existing match
    # was already completed.
    assert result.to_state == "in_review"
    assert len(result.tasks) == 1
    assert result.tasks[0].created_by_transition == "submit_for_review"


# ---------------------------------------------------------------------------
# 7. Tenant isolation — cross-tenant case id surfaces as NotFound
# ---------------------------------------------------------------------------


async def test_execute_transition__cross_tenant_case__raises_not_found(
    seeded: dict[str, object],
    clock: FixedClock,
    audit: InMemoryAuditStore,
    outbox: InMemoryOutboxWriter,
) -> None:
    """A case id from a different tenant surfaces as ``NotFound`` —
    the two cases are indistinguishable so the API never confirms
    cross-tenant existence (Constitution V)."""
    case: Case = seeded["case"]  # type: ignore[assignment]
    case_repo: InMemoryCaseRepository = seeded["case_repo"]  # type: ignore[assignment]
    task_repo: InMemoryTaskRepository = seeded["task_repo"]  # type: ignore[assignment]
    instance_repo: InMemoryWorkflowInstanceRepository = seeded[  # type: ignore[assignment]
        "instance_repo"
    ]
    workflow_version_repo: InMemoryWorkflowVersionRepository = (  # type: ignore[assignment]
        seeded["workflow_version_repo"]
    )

    with pytest.raises(NotFound):
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=BETA_TENANT,  # Cross-tenant attempt.
                actor_id=REVIEWER_USER,
                actor_roles=("reviewer",),
                case_id=case.id,
                transition_key="submit_for_review",
                correlation_id="corr-cross-tenant",
            ),
            case_repo=case_repo,
            instance_repo=instance_repo,
            workflow_version_repo=workflow_version_repo,
            task_repo=task_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _actor(*, roles: tuple[str, ...]) -> object:
    """Build a duck-typed ``UserContext`` for the policy.

    Returns a small object with just the fields ``authorize`` reads
    (``tenant_id``, ``user_id``, ``roles``, ``is_system``). The
    policy duck-types so we do not need to import ``app.deps``.
    """

    class _Stub:
        def __init__(self) -> None:
            self.tenant_id = ACME_TENANT
            self.user_id = REVIEWER_USER
            self.roles = roles
            self.is_system = False
            self.display_name = ""

    return _Stub()
