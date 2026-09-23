"""SLA breach-action evaluation + approval self-approval block (FM-017 / FM-018).

Covers the T4-US2/T4-US3 acceptance for this task:

- Originator self-approval on an ``approval_required`` transition is
  blocked with ``Forbidden`` and an explainable audit payload lands
  on the successful second-actor attempt.
- Breach fires each configured escalation deterministically
  (``notify``, ``supervisor_task``, ``queue_move``,
  ``exception_state``) with the outcome visible in the audit
  timeline payload.

Naming: test_<unit>_<scenario>__<expectation>, given/when/then.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application import authorization as authz_mod
from app.application.authorization import Policy, configure_default_policy
from app.application.configuration.validate_workflow_definition import validate_definition
from app.application.sla import scan_sla
from app.application.sla.scan_sla import describe_escalation
from app.application.workflow import execute_transition as transition_uc
from app.application.workflow.execute_transition import ExecuteTransitionCommand
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.errors import Forbidden, ValidationFailed
from app.domain.sla import SlaClock
from app.domain.task import Task
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


@pytest.fixture(autouse=True)
def _reset_default_policy():  # type: ignore[no-untyped-def]
    original = authz_mod._default_policy
    configure_default_policy(Policy())
    yield
    configure_default_policy(original)


TENANT = UUID("11111111-1111-1111-1111-111111111111")
ORIGINATOR = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
APPROVER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Approval helpers
# ---------------------------------------------------------------------------


async def _approval_setup() -> tuple[
    InMemoryCaseRepository,
    InMemoryTaskRepository,
    InMemoryWorkflowInstanceRepository,
    InMemoryWorkflowVersionRepository,
    Case,
]:
    """Seed one case + approval-gated workflow version (originator known)."""
    case_repo = InMemoryCaseRepository()
    task_repo = InMemoryTaskRepository()
    instance_repo = InMemoryWorkflowInstanceRepository()
    version_repo = InMemoryWorkflowVersionRepository()
    definition = WorkflowDefinition.create(tenant_id=TENANT, key="W", name="W")
    version = WorkflowVersion.draft(
        tenant_id=TENANT,
        workflow_definition_id=definition.id,
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="in_review", label="Review"),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="decide",
                from_state="intake",
                to_state="in_review",
                human_driven=True,
                approval_required=True,
                eligible_approver_roles=("supervisor",),
            ),
            WorkflowTransition(
                key="close", from_state="in_review", to_state="closed", human_driven=True
            ),
        ),
    ).publish(actor_id=uuid4(), at=NOW)
    version_repo._by_id[version.id] = version
    ctv = CaseTypeVersion(
        id=uuid4(),
        tenant_id=TENANT,
        case_type_id=uuid4(),
        version_number=1,
        schema=CaseTypeSchema(
            fields=(CaseTypeField(key="title", label="Title", type="text", required=True),)
        ),
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=NOW,
        published_by=ORIGINATOR,
    )
    case = Case.create(
        tenant_id=TENANT,
        reference=CaseReference(value="C-ACME-2026-000042"),
        case_type_version=ctv,
        title="t",
        description="",
        data={"title": "t"},
        priority="normal",
        created_by=ORIGINATOR,
        now=NOW,
        sensitivity="internal",
    )
    case_repo.add(case)
    instance_repo.add(
        StoredInstance(
            id=uuid4(),
            tenant_id=TENANT,
            case_id=case.id,
            workflow_definition_id=definition.id,
            workflow_version_id=version.id,
            current_state="intake",
            correlation_id="c",
            version_token=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return case_repo, task_repo, instance_repo, version_repo, case


async def test_approval__originator_self_approval__blocked() -> None:
    """The case originator cannot execute their own consequential transition."""
    # given
    case_repo, task_repo, instance_repo, version_repo, case = await _approval_setup()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    # when — originator attempts the approval-gated transition
    with pytest.raises(Forbidden, match="separation_of_duties_violation"):
        await transition_uc.handle(
            ExecuteTransitionCommand(
                tenant_id=TENANT,
                actor_id=ORIGINATOR,
                actor_roles=("supervisor",),
                case_id=case.id,
                transition_key="decide",
                correlation_id="corr-self",
            ),
            case_repo=case_repo,
            instance_repo=instance_repo,
            workflow_version_repo=version_repo,
            task_repo=task_repo,
            audit=audit,
            outbox=outbox,
            clock=FixedClock(NOW),
        )
    # then — denied attempt stages no state change, no
    # WorkflowTransitioned row, no outbox row (no partial commit) — but
    # exactly one AccessDecisionRecorded denial row (FM-045 stream).
    assert outbox.records == []
    assert len(audit.events) == 1
    denial = audit.events[0]
    assert denial.event_type == "AccessDecisionRecorded"
    assert denial.payload["result"] == "denied"
    assert denial.payload["reason"] == "separation_of_duties_violation"
    assert denial.payload["sod_outcome"] == "denied_originator"
    assert denial.payload["transition_key"] == "decide"
    stored = await instance_repo.get_for_case(TENANT, case.id)
    assert stored is not None
    assert stored.current_state == "intake"


async def test_approval__second_actor__succeeds_with_explainable_audit() -> None:
    """An eligible second actor executes; the audit row explains the SoD decision."""
    # given
    case_repo, task_repo, instance_repo, version_repo, case = await _approval_setup()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    # when — a different supervisor executes the same transition
    result = await transition_uc.handle(
        ExecuteTransitionCommand(
            tenant_id=TENANT,
            actor_id=APPROVER,
            actor_roles=("supervisor",),
            case_id=case.id,
            transition_key="decide",
            correlation_id="corr-second",
        ),
        case_repo=case_repo,
        instance_repo=instance_repo,
        workflow_version_repo=version_repo,
        task_repo=task_repo,
        audit=audit,
        outbox=outbox,
        clock=FixedClock(NOW),
    )
    # then — state moved and exactly one explainable audit row landed
    assert result.to_state == "in_review"
    assert len(audit.events) == 1
    payload = audit.events[0].payload
    assert payload["approval_required"] is True
    assert payload["sod_outcome"] == "allowed"
    assert payload["sod_reason"] != ""
    assert payload["originator_id"] == str(ORIGINATOR)
    assert payload["approver_id"] == str(APPROVER)


# ---------------------------------------------------------------------------
# SLA scanner stubs
# ---------------------------------------------------------------------------


class _ScanClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class _ClockRepo:
    def __init__(self, clocks: list[SlaClock]) -> None:
        self._clocks = {c.id: c for c in clocks}

    async def list_due(self, tenant_id: UUID, *, now: datetime, limit: int = 100) -> list[SlaClock]:
        return [c for c in self._clocks.values() if c.is_overdue(at=now)][:limit]

    async def list_counting(
        self, tenant_id: UUID, *, now: datetime, limit: int = 100
    ) -> list[SlaClock]:
        return [c for c in self._clocks.values() if c.is_counting()][:limit]

    async def update(self, clock: SlaClock) -> None:
        self._clocks[clock.id] = clock

    async def add(self, clock: SlaClock) -> None:
        self._clocks[clock.id] = clock


class _Audit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class _Outbox:
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def enqueue(self, **kwargs: Any) -> None:
        self.records.append(kwargs)


class _Cases:
    async def is_under_legal_hold(self, tenant_id: UUID, case_id: UUID) -> bool:
        return False


class _Tasks:
    def __init__(self) -> None:
        self.created: list[Task] = []

    async def add(self, task: Task) -> None:
        self.created.append(task)

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID,
        task_type: str,
        title: str,
        priority: str,
        queue_key: str | None,
    ) -> Task:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return Task.create(
            tenant_id=tenant_id,
            case_id=case_id,
            type=task_type,
            title=title,
            priority=priority,
            assignee_queue_id=queue_key,
            created_at=now,
        )


def _overdue_clock(
    tenant: UUID, case_id: UUID, now: datetime, escalation: dict[str, Any]
) -> SlaClock:
    return SlaClock.start(
        tenant_id=tenant,
        case_id=case_id,
        clock_key="review_due",
        started_at=now - timedelta(hours=72),
        due_at=now - timedelta(hours=1),
        escalation_on_breach=escalation,
    )


async def _scan(escalation: dict[str, Any]) -> tuple[_ClockRepo, _Audit, _Outbox, _Tasks, Any]:
    tenant = uuid4()
    case_id = uuid4()
    now = datetime(2026, 1, 10, tzinfo=UTC)
    repo = _ClockRepo([_overdue_clock(tenant, case_id, now, escalation)])
    audit = _Audit()
    outbox = _Outbox()
    tasks = _Tasks()
    result = await scan_sla.scan_once(
        tenant_id=tenant,
        clock_repo=repo,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        outbox=outbox,  # type: ignore[arg-type]
        case_lookup=_Cases(),  # type: ignore[arg-type]
        task_creator=tasks,  # type: ignore[arg-type]
        task_repo=tasks,  # type: ignore[arg-type]
        clock=_ScanClock(now),
    )
    return repo, audit, outbox, tasks, result


async def test_breach__supervisor_task__creates_task_and_audits_target() -> None:
    """A supervisor_task escalation creates a supervisor task and audits the route."""
    # given — an overdue clock routed to the supervisor queue
    # when
    _, audit, _, tasks, result = await _scan({"type": "supervisor_task", "queue": "supq"})
    # then — one supervisor task exists and the timeline names the route
    assert len(result.breached) == 1
    assert len(tasks.created) == 1
    assert tasks.created[0].type == "supervisor_review"
    assert tasks.created[0].assignee_queue_id == "supq"
    payload = audit.events[0]["payload"]
    assert payload["escalation_fired"] is True
    assert payload["escalation_type"] == "supervisor_task"
    assert payload["escalation_target"] == "supq"


async def test_breach__queue_move__records_routing_intent() -> None:
    """A queue_move escalation is recorded deterministically without mutating tasks."""
    # given / when
    _, audit, outbox, tasks, result = await _scan({"type": "queue_move", "target_queue": "supq"})
    # then — intent recorded, no task side effect, timeline + outbox agree
    assert len(result.breached) == 1
    assert tasks.created == []
    payload = audit.events[0]["payload"]
    assert payload["escalation_fired"] is True
    assert payload["escalation_type"] == "queue_move"
    assert payload["escalation_target"] == "supq"
    assert outbox.records[0]["payload"]["escalation_target"] == "supq"


async def test_breach__exception_state__records_routing_intent() -> None:
    """An exception_state escalation is recorded deterministically in the timeline."""
    # given / when
    _, audit, _, tasks, result = await _scan(
        {"type": "exception_state", "exception_state": "needs_review"}
    )
    # then
    assert len(result.breached) == 1
    assert tasks.created == []
    payload = audit.events[0]["payload"]
    assert payload["escalation_fired"] is True
    assert payload["escalation_type"] == "exception_state"
    assert payload["escalation_target"] == "needs_review"


async def test_breach__notify__deterministic_no_action() -> None:
    """Notify stays a deterministic no-op placeholder with a visible audit row."""
    # given / when — same shape scanned once
    _, audit, _, tasks, _ = await _scan({"type": "notify", "queue": "q"})
    # then — nothing created, audit still names the escalation
    assert tasks.created == []
    payload = audit.events[0]["payload"]
    assert payload["escalation_fired"] is False
    assert payload["escalation_type"] == "notify"
    assert payload["escalation_target"] == "q"
    # then — pure description repeats identically (determinism contract)
    assert describe_escalation({"type": "notify", "queue": "q"}) == ("notify", "q")
    assert describe_escalation({"type": "queue_move", "target_queue": "supq"}) == (
        "queue_move",
        "supq",
    )
    assert describe_escalation({"type": "exception_state", "exception_state": "x"}) == (
        "exception_state",
        "x",
    )
    assert describe_escalation({"type": "supervisor_task"}) == (
        "supervisor_task",
        "supervisor_queue",
    )
    assert describe_escalation(None) == (None, None)


async def test_breach__misconfigured_routing__fails_closed() -> None:
    """Blank queue_move / exception_state targets fire nothing but still audit."""
    # given / when
    _, audit, _, tasks, _ = await _scan({"type": "queue_move", "target_queue": "  "})
    # then — fail-closed, timeline keeps the raw type for diagnosis
    assert tasks.created == []
    assert audit.events[0]["payload"]["escalation_fired"] is False
    assert audit.events[0]["payload"]["escalation_type"] == "queue_move"


def _sla_version_with(escalation: dict[str, Any]) -> WorkflowVersion:
    definition = WorkflowDefinition.create(tenant_id=TENANT, key="W", name="W")
    return WorkflowVersion.draft(
        tenant_id=TENANT,
        workflow_definition_id=definition.id,
        version_number=1,
        states=(
            WorkflowState(key="intake", label="Intake", is_initial=True),
            WorkflowState(key="in_review", label="Review"),
            WorkflowState(key="closed", label="Closed", is_terminal=True),
        ),
        transitions=(
            WorkflowTransition(
                key="submit",
                from_state="intake",
                to_state="in_review",
                human_driven=True,
                actions=(
                    {
                        "type": "start_sla",
                        "sla_key": "review_due",
                        "hours": 48,
                        "queue": "q",
                        "escalation_on_breach": escalation,
                    },
                ),
            ),
            WorkflowTransition(
                key="close", from_state="in_review", to_state="closed", human_driven=True
            ),
        ),
    )


def test_validate__new_escalation_types__accepted() -> None:
    """Publish gate accepts all four breach-action shapes."""
    # given / when / then — no raise for any supported type
    validate_definition(_sla_version_with({"type": "supervisor_task"}), known_queues=("q",))
    validate_definition(
        _sla_version_with({"type": "queue_move", "target_queue": "q"}),
        known_queues=("q",),
    )
    validate_definition(
        _sla_version_with({"type": "exception_state", "exception_state": "needs_review"}),
        known_queues=("q",),
    )
    validate_definition(_sla_version_with({"type": "notify", "queue": "q"}), known_queues=("q",))


def test_validate__routing_without_target__rejected() -> None:
    """Publish gate rejects queue_move / exception_state without a target."""
    # given / when / then
    with pytest.raises(ValidationFailed, match="target_queue"):
        validate_definition(_sla_version_with({"type": "queue_move"}), known_queues=("q",))
    with pytest.raises(ValidationFailed, match="exception_state"):
        validate_definition(_sla_version_with({"type": "exception_state"}), known_queues=("q",))
