"""Transition action firing: set_field + start_sla (T071).

``execute_transition`` fires every configured action type in the same
transaction: ``create_task`` (covered in ``test_execute_transition``),
``set_field`` (case field/data writes via ``Case.edit``), and
``start_sla`` (deadline clocks, skipped when a live clock with the
same key already runs). Authoring-shape guards (``{"op": ...}``) are
also exercised here to prove the T067 unification at runtime.

Fakes are reused from ``workflow._execute_transition_fakes``; the SLA
store is local (add + list_for_case only).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application import authorization as authz_mod
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
from app.domain.sla import SlaClock, SlaClockStatus
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

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
REVIEWER_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_default_policy() -> Any:
    original = authz_mod._default_policy
    configure_default_policy(Policy())
    yield
    configure_default_policy(original)


class _SlaStore:
    """Minimal SlaClockRepository: add + list_for_case."""

    def __init__(self, clocks: list[SlaClock] | None = None) -> None:
        self.clocks: list[SlaClock] = list(clocks or [])

    async def add(self, clock: SlaClock) -> None:
        self.clocks.append(clock)

    async def list_for_case(self, tenant_id: UUID, case_id: UUID) -> list[SlaClock]:
        return [c for c in self.clocks if c.tenant_id == tenant_id and c.case_id == case_id]


class _VersionRepo:
    """Minimal CaseTypeVersionRepository holding one version."""

    def __init__(self, version: CaseTypeVersion) -> None:
        self._version = version

    async def get(self, tenant_id: UUID, version_id: UUID) -> CaseTypeVersion | None:
        if tenant_id == self._version.tenant_id and version_id == self._version.id:
            return self._version
        return None


def _actor(*, roles: tuple[str, ...] = ("reviewer",)) -> object:
    class _Stub:
        def __init__(self) -> None:
            self.tenant_id = ACME_TENANT
            self.user_id = REVIEWER_USER
            self.roles = roles
            self.is_system = False
            self.display_name = ""

    return _Stub()


def _case_type_version() -> CaseTypeVersion:
    return CaseTypeVersion(
        id=UUID("33333333-3333-3333-3333-333333333333"),
        tenant_id=ACME_TENANT,
        case_type_id=uuid4(),
        version_number=1,
        schema=CaseTypeSchema(
            fields=(
                CaseTypeField(key="title", label="Title", type="text", required=True),
                CaseTypeField(key="summary", label="Summary", type="text", required=False),
            )
        ),
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=FIXED_NOW,
        published_by=REVIEWER_USER,
    )


def _case(version: CaseTypeVersion, priority: str = "normal") -> Case:
    return Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference(value="C-ACME-2026-000001"),
        case_type_version=version,
        title="Service request",
        description="",
        data={"title": "Service request"},
        priority=priority,
        created_by=REVIEWER_USER,
        now=FIXED_NOW,
        sensitivity="internal",
    )


def _version(
    actions: tuple[dict[str, Any], ...], guard: dict[str, Any] | None = None
) -> WorkflowVersion:
    definition_id = uuid4()
    return WorkflowVersion.draft(
        tenant_id=ACME_TENANT,
        workflow_definition_id=definition_id,
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="in_review", label="In review"),
            WorkflowState(key="decided", label="Decided", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="submit_for_review",
                from_state="intake",
                to_state="in_review",
                human_driven=True,
                guard=guard,
                actions=actions,
            ),
        ),
    )


async def _seed(
    actions: tuple[dict[str, Any], ...],
    *,
    guard: dict[str, Any] | None = None,
    priority: str = "normal",
    sla: _SlaStore | None = None,
) -> dict[str, Any]:
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    instance_repo = InMemoryWorkflowInstanceRepository()
    version_repo = InMemoryWorkflowVersionRepository()
    definition = WorkflowDefinition.create(
        tenant_id=ACME_TENANT, key="ServiceRequestWorkflow", name="Service Request"
    )
    version = _version(actions, guard).publish(actor_id=REVIEWER_USER, at=FIXED_NOW)
    await version_repo.add(version)
    type_version = _case_type_version()
    case = _case(type_version, priority)
    case_repo.add(case)
    instance_repo.add(
        StoredInstance(
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
    )
    return {
        "case": case,
        "version": version,
        "definition": definition,
        "case_repo": case_repo,
        "task_repo": task_repo,
        "instance_repo": instance_repo,
        "version_repo": version_repo,
        "type_version_repo": _VersionRepo(type_version),
        "sla": sla or _SlaStore(),
        "audit": InMemoryAuditStore(),
        "outbox": InMemoryOutboxWriter(),
    }


async def _run(ctx: dict[str, Any], **overrides: Any) -> Any:
    case: Case = ctx["case"]  # type: ignore[assignment]
    await authorize(
        Action.CASE_TRANSITION,
        _actor(),
        Resource(tenant_id=ACME_TENANT, kind="case", id=case.id),
    )
    cmd = ExecuteTransitionCommand(
        tenant_id=ACME_TENANT,
        actor_id=REVIEWER_USER,
        actor_roles=("reviewer",),
        case_id=case.id,
        transition_key="submit_for_review",
        correlation_id="corr-a",
        **overrides,
    )
    return await transition_uc.handle(
        cmd,
        case_repo=ctx["case_repo"],
        instance_repo=ctx["instance_repo"],
        workflow_version_repo=ctx["version_repo"],
        task_repo=ctx["task_repo"],
        audit=ctx["audit"],
        outbox=ctx["outbox"],
        clock=FixedClock(FIXED_NOW),
        sla_clock_repo=ctx["sla"],
        case_type_version_repo=ctx["type_version_repo"],
    )


async def test_transition__set_field__writes_data_and_top_level() -> None:
    """`set_field` writes a data key and a top-level field atomically."""
    # // given
    ctx = await _seed(
        (
            {"type": "set_field", "field": "summary", "value": "triage done"},
            {"type": "set_field", "field": "priority", "value": "high"},
        )
    )

    # // when
    result = await _run(ctx)

    # // then
    assert result.case.data["summary"] == "triage done"
    assert result.case.priority == "high"
    stored = await ctx["case_repo"].get(ACME_TENANT, ctx["case"].id)
    assert stored is not None
    assert stored.data["summary"] == "triage done"
    payload = ctx["audit"].events[0].payload
    assert payload["fields_written"] == {"summary": "triage done", "priority": "high"}
    assert payload["sla_started"] == []


async def test_transition__set_field_malformed__skips_and_succeeds() -> None:
    """An action without a field is skipped; the state move still lands."""
    # // given
    ctx = await _seed(({"type": "set_field", "value": "orphan"},))

    # // when
    result = await _run(ctx)

    # // then
    assert result.to_state == "in_review"
    assert ctx["audit"].events[0].payload["fields_written"] == {}


async def test_transition__start_sla__stages_clock_and_reports_key() -> None:
    """`start_sla` stages a deadline clock in the same transaction."""
    # // given
    ctx = await _seed(
        ({"type": "start_sla", "sla_key": "review_due", "hours": 48, "queue": "review_queue"},)
    )

    # // when
    await _run(ctx)

    # // then
    sla: _SlaStore = ctx["sla"]
    assert [c.clock_key for c in sla.clocks] == ["review_due"]
    assert sla.clocks[0].status is SlaClockStatus.ACTIVE
    assert ctx["audit"].events[0].payload["sla_started"] == ["review_due"]


async def test_transition__start_sla_live_clock__skips_restart() -> None:
    """A live clock with the same key is not restarted."""
    # // given: an active clock already runs for this key
    ctx = await _seed(
        ({"type": "start_sla", "sla_key": "review_due", "hours": 48, "queue": "review_queue"},)
    )
    case: Case = ctx["case"]
    ctx["sla"].clocks.append(
        SlaClock.start(
            tenant_id=ACME_TENANT,
            case_id=case.id,
            clock_key="review_due",
            started_at=FIXED_NOW,
            due_at=datetime(2026, 9, 5, tzinfo=UTC),
        )
    )

    # // when
    await _run(ctx)

    # // then: no second clock, audit reports none started
    assert len(ctx["sla"].clocks) == 1
    assert ctx["audit"].events[0].payload["sla_started"] == []


async def test_transition__start_sla_terminal_clock__starts_fresh() -> None:
    """A breached clock does not block a fresh deadline for the new phase."""
    # // given
    ctx = await _seed(
        ({"type": "start_sla", "sla_key": "review_due", "hours": 48, "queue": "review_queue"},)
    )
    case: Case = ctx["case"]
    old = SlaClock.start(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        clock_key="review_due",
        started_at=datetime(2026, 8, 1, tzinfo=UTC),
        due_at=datetime(2026, 8, 2, tzinfo=UTC),
    ).mark_breached(at=datetime(2026, 8, 3, tzinfo=UTC))
    ctx["sla"].clocks.append(old)

    # // when
    await _run(ctx)

    # // then
    assert len(ctx["sla"].clocks) == 2
    assert ctx["audit"].events[0].payload["sla_started"] == ["review_due"]


async def test_transition__authoring_guard_false__blocks() -> None:
    """Authoring-shape guards evaluate (T067 unification at runtime)."""
    # // given: priority is normal, guard demands high
    ctx = await _seed((), guard={"op": "eq", "field": "case.priority", "value": "high"})

    # // when / then
    with pytest.raises(GuardBlocked):
        await _run(ctx)
    assert ctx["audit"].events == []


async def test_transition__authoring_guard_true__passes() -> None:
    """A satisfied authoring-shape guard lets the transition through."""
    # // given
    ctx = await _seed(
        (),
        guard={"op": "eq", "field": "case.priority", "value": "high"},
        priority="high",
    )

    # // when
    result = await _run(ctx)

    # // then
    assert result.to_state == "in_review"
    assert [e.event_type for e in ctx["audit"].events] == [WORKFLOW_TRANSITIONED]
