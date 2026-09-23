"""Application tests for ``app.application.sla.scan_sla`` (P2-US6 / FM-18).

Acceptance scenarios (AGENTS.md §4.R5, FM-18 acceptance):

1. **Breach fires escalation** — a 48-hour SLA clock past its
   deadline is flipped to ``breached``; the configured
   ``create_task`` action materializes a new task; an
   ``SlaBreached`` audit row + ``SlaBreached`` outbox row land
   in the same transaction.
2. **Worker sees the warning tier first** — a clock inside its
   ``warning_at..due_at`` window is flipped to ``warning``; an
   ``SlaWarningRaised`` audit row + outbox row land and the
   configured warning escalation fires. The next scanner tick
   is a no-op (idempotent).
3. **Non-breached clock stays active** — a counting clock well
   ahead of its deadline is left alone by both passes; no
   audit or outbox rows.
4. **Timeline visibility** — the audit rows carry the clock's
   stable key, ``due_at``, and the escalation outcome so the
   timeline can render "SLA breached at <ts> for clock
   ``review_due``" without re-reading the clock.
5. **Idempotent scanner** — running the breach pass twice in a
   row does not double-write audit rows; the aggregate's
   ``mark_breached`` is the dedup layer.

Naming follows AGENTS.md §4.R5: tests are sortable
(``pytest --collect-only``) and filterable (``-k``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.application.sla import scan_sla
from app.application.sla.scan_sla import (
    ScanSlaResult,
    ScanWarningResult,
    scan_once,
    scan_warning_once,
)
from app.domain.audit import SLA_BREACHED, SLA_WARNING_RAISED
from app.domain.sla import SlaClock, SlaClockStatus
from app.domain.task import Task

# ---------------------------------------------------------------------------
# Fixed inputs
# ---------------------------------------------------------------------------

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
BETA_TENANT = UUID("22222222-2222-2222-2222-222222222222")
CASE_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
OTHER_CASE_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

T0 = datetime(226, 8, 31, 12, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


# ---------------------------------------------------------------------------
# Fakes (mirrors ``tests/application/workflow/_execute_transition_fakes.py``)
# ---------------------------------------------------------------------------


class FixedClock:
    """A ``Clock`` Protocol implementation that returns a fixed UTC instant."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or T0

    def now(self) -> datetime:
        return self._now


@dataclass
class StagedAudit:
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    actor_type: str
    subject_type: str
    subject_id: UUID
    case_id: UUID | None
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    occurred_at: datetime


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.events: list[StagedAudit] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        self.events.append(
            StagedAudit(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                actor_type=actor_type,
                subject_type=subject_type,
                subject_id=subject_id,
                case_id=case_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        )


@dataclass
class StagedOutbox:
    tenant_id: UUID
    event_type: str
    event_id: UUID
    payload: dict[str, Any]


class InMemoryOutboxWriter:
    def __init__(self) -> None:
        self.records: list[StagedOutbox] = []

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        self.records.append(
            StagedOutbox(
                tenant_id=tenant_id,
                event_type=event_type,
                event_id=event_id,
                payload=payload,
            )
        )


class InMemorySlaClockRepository:
    """In-memory ``SlaClockRepository``.

    Mirrors the SQL adapter's tenant isolation
    (``WHERE tenant_id = :tenant_id``) and counting-state filter
    for ``list_due``. ``list_for_case`` is provided for the
    case-summary projection.
    """

    def __init__(self) -> None:
        self._clocks: dict[UUID, SlaClock] = {}
        self.updates: list[SlaClock] = []

    def add(self, clock: SlaClock) -> None:
        self._clocks[clock.id] = clock

    async def get(self, tenant_id: UUID, clock_id: UUID) -> SlaClock | None:
        clock = self._clocks.get(clock_id)
        if clock is None or clock.tenant_id != tenant_id:
            return None
        return clock

    async def update(self, clock: SlaClock) -> None:
        self.updates.append(clock)
        self._clocks[clock.id] = clock

    async def list_due(
        self,
        tenant_id: UUID,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[SlaClock]:
        counting = {
            SlaClockStatus.ACTIVE,
            SlaClockStatus.RUNNING,
            SlaClockStatus.WARNING,
        }
        rows = [
            c
            for c in self._clocks.values()
            if c.tenant_id == tenant_id and c.status in counting and c.due_at <= now
        ]
        rows.sort(key=lambda c: c.due_at)
        return rows[:limit]

    async def list_counting(
        self,
        tenant_id: UUID,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[SlaClock]:
        counting = {
            SlaClockStatus.ACTIVE,
            SlaClockStatus.RUNNING,
            SlaClockStatus.WARNING,
        }
        rows = [
            c for c in self._clocks.values() if c.tenant_id == tenant_id and c.status in counting
        ]
        rows.sort(key=lambda c: c.due_at)
        return rows[:limit]

    async def list_for_case(self, tenant_id: UUID, case_id: UUID) -> list[SlaClock]:
        return [
            c for c in self._clocks.values() if c.tenant_id == tenant_id and c.case_id == case_id
        ]


class InMemoryTaskRepository:
    def __init__(self) -> None:
        self._tasks: list[Task] = []

    async def add(self, task: Task) -> None:
        self._tasks.append(task)

    async def get(self, tenant_id: UUID, task_id: UUID) -> Task | None:
        for t in self._tasks:
            if t.tenant_id == tenant_id and t.id == task_id:
                return t
        return None


class InMemoryCaseLookup:
    """Stand-in for the case legal-hold check (FM-39 seam)."""

    def __init__(self, *, held_cases: set[UUID] | None = None) -> None:
        self._held = held_cases or set()

    async def is_under_legal_hold(self, tenant_id: UUID, case_id: UUID) -> bool:
        return case_id in self._held


def _make_task_creator():
    """Return a closure-shaped ``_TaskCreator`` + the tasks it produced.

    Mirrors the production shape: the scanner does not import
    ``app.application.tasks``; the boundary wires a creator at
    runtime. The closure returns the task and stashes it in a
    list the test can inspect.
    """

    created: list[Task] = []

    async def creator(
        *,
        tenant_id: UUID,
        case_id: UUID,
        task_type: str,
        title: str,
        priority: str,
        queue_key: str | None,
    ) -> Task:
        task = Task.create(
            tenant_id=tenant_id,
            case_id=case_id,
            type=task_type,
            title=title,
            priority=priority,
            assignee_queue_id=queue_key,
        )
        created.append(task)
        return task

    return creator, created


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_clock(
    *,
    tenant_id: UUID = ACME_TENANT,
    case_id: UUID = CASE_ID,
    clock_key: str = "review_due",
    started_at: datetime = T0,
    due_at: datetime = T0 + 48 * HOUR,
    warning_at: datetime | None = None,
    status: SlaClockStatus = SlaClockStatus.ACTIVE,
    escalation_on_warning: dict[str, Any] | None = None,
    escalation_on_breach: dict[str, Any] | None = None,
    breach_action: dict[str, Any] | None = None,
) -> SlaClock:
    """Build an SlaClock with the requested shape.

    Defaults: 48h deadline, ``active`` status, no warning tier.
    Override ``status`` for pre-breached scenarios.
    """
    return SlaClock(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id,
        clock_key=clock_key,
        started_at=started_at,
        due_at=due_at,
        status=status,
        paused_at=None,
        warning_at=warning_at,
        breach_action=breach_action,
        escalation_on_warning=escalation_on_warning,
        escalation_on_breach=escalation_on_breach,
        metadata=None,
    )


# ---------------------------------------------------------------------------
# 1. Breach fires the configured escalation
# ---------------------------------------------------------------------------


async def test_scan_once__deadline_breach_creates_escalation_audit_and_timeline() -> None:
    """A 48h SLA clock past its deadline is flipped to ``breached``,
    the configured ``create_task`` action materializes a follow-on
    task, and an ``SlaBreached`` audit row + outbox row land with
    the clock's stable key + due_at so the timeline can render
    "SLA breached at <ts> for clock review_due" (P2-US6 / FM-18).
    """
    clock_repo = InMemorySlaClockRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    creator, created = _make_task_creator()

    clock = _make_clock(
        escalation_on_breach={
            "type": "create_task",
            "task_type": "escalation",
            "title": "SLA breach: review_due",
            "priority": "high",
            "queue": "supervisor_queue",
        }
    )
    clock_repo.add(clock)

    now = clock.due_at + timedelta(minutes=1)
    result = await scan_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=FixedClock(now=now),
    )

    # 1. The result reports the breach.
    assert isinstance(result, ScanSlaResult)
    assert result.breached == [clock.id]
    assert result.skipped_holds == 0
    assert result.considered == 1

    # 2. The clock is now breached on the repository.
    stored = await clock_repo.get(ACME_TENANT, clock.id)
    assert stored is not None
    assert stored.status is SlaClockStatus.BREACHED

    # 3. The configured escalation action created a task on the case.
    assert len(created) == 1
    task = created[0]
    assert task.type == "escalation"
    assert task.case_id == CASE_ID
    assert task.assignee_queue_id == "supervisor_queue"
    assert task.priority == "high"

    # 4. Exactly one ``SlaBreached`` audit row landed; the timeline
    # fields are populated from the scanner's read of the clock so
    # the operator sees the same deadline the engine did.
    breach_events = [e for e in audit.events if e.event_type == SLA_BREACHED]
    assert len(breach_events) == 1
    row = breach_events[0]
    assert row.subject_id == clock.id
    assert row.case_id == CASE_ID
    assert row.actor_type == "system"
    assert row.payload["clock_key"] == "review_due"
    assert row.payload["due_at"] == clock.due_at.isoformat()
    assert row.payload["breached_at"] == now.isoformat()
    assert row.payload["escalation_fired"] is True
    assert row.payload["case_under_legal_hold"] is False

    # 5. Exactly one matching outbox row landed.
    outbox_events = [
        r for r in outbox.records if r.event_type == scan_sla.OUTBOX_EVENT_SLA_BREACHED
    ]
    assert len(outbox_events) == 1
    assert outbox_events[0].payload["clock_key"] == "review_due"
    assert outbox_events[0].payload["escalation_fired"] is True


async def test_scan_once__is_idempotent_on_replay() -> None:
    """Re-running the breach pass against an already-breached clock
    does not double-write audit rows. The aggregate's
    ``mark_breached`` returns the same shape on the second call so
    the scanner can be retried at any cadence.
    """
    clock_repo = InMemorySlaClockRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    creator, created = _make_task_creator()

    clock = _make_clock(
        escalation_on_breach={
            "type": "create_task",
            "task_type": "escalation",
            "title": "SLA breach",
            "queue": "supervisor_queue",
        }
    )
    clock_repo.add(clock)
    now = clock.due_at + timedelta(minutes=1)
    clock_obj = FixedClock(now=now)

    first = await scan_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=clock_obj,
    )
    assert first.breached == [clock.id]

    second = await scan_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=clock_obj,
    )
    assert second.breached == []
    assert second.considered == 0
    assert len([e for e in audit.events if e.event_type == SLA_BREACHED]) == 1
    assert len(created) == 1


# ---------------------------------------------------------------------------
# 2. Warning tier — worker sees "warning" then "breached"
# ---------------------------------------------------------------------------


async def test_scan_warning_once__deadline_window_creates_warning_audit_and_timeline() -> None:
    """A clock inside its ``warning_at..due_at`` window is flipped
    to ``warning``; the configured ``escalation_on_warning``
    action materializes a task; an ``SlaWarningRaised`` audit row
    + outbox row land so the timeline shows the worker
    approaching the deadline (P2-US6 / FM-18).
    """
    clock_repo = InMemorySlaClockRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    creator, created = _make_task_creator()

    clock = _make_clock(
        warning_at=T0 + 42 * HOUR,
        due_at=T0 + 48 * HOUR,
        escalation_on_warning={
            "type": "create_task",
            "task_type": "supervisor_review",
            "title": "SLA warning: review_due",
            "priority": "normal",
            "queue": "supervisor_queue",
        },
    )
    clock_repo.add(clock)

    now = T0 + 45 * HOUR
    result = await scan_warning_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=FixedClock(now=now),
    )

    assert isinstance(result, ScanWarningResult)
    assert result.warned == [clock.id]
    assert result.skipped_holds == 0

    stored = await clock_repo.get(ACME_TENANT, clock.id)
    assert stored is not None
    assert stored.status is SlaClockStatus.WARNING

    assert len(created) == 1
    assert created[0].type == "supervisor_review"

    warn_events = [e for e in audit.events if e.event_type == SLA_WARNING_RAISED]
    assert len(warn_events) == 1
    row = warn_events[0]
    assert row.payload["clock_key"] == "review_due"
    assert row.payload["warning_at"] == clock.warning_at.isoformat()
    assert row.payload["due_at"] == clock.due_at.isoformat()
    assert row.payload["escalation_fired"] is True

    outbox_events = [
        r for r in outbox.records if r.event_type == scan_sla.OUTBOX_EVENT_SLA_WARNING_RAISED
    ]
    assert len(outbox_events) == 1
    assert outbox_events[0].payload["clock_key"] == "review_due"


async def test_scan_warning_once__is_idempotent_on_replay() -> None:
    """Re-running the warning pass against an already-warning clock
    does not double-write audit rows. The aggregate's
    ``mark_warning`` is the dedup layer.
    """
    clock_repo = InMemorySlaClockRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    creator, created = _make_task_creator()

    clock = _make_clock(
        warning_at=T0 + 42 * HOUR,
        due_at=T0 + 48 * HOUR,
        escalation_on_warning={
            "type": "create_task",
            "task_type": "supervisor_review",
            "queue": "supervisor_queue",
        },
    )
    clock_repo.add(clock)
    now = T0 + 45 * HOUR
    clock_obj = FixedClock(now=now)

    first = await scan_warning_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=clock_obj,
    )
    assert first.warned == [clock.id]

    second = await scan_warning_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=clock_obj,
    )
    assert second.warned == []
    assert len([e for e in audit.events if e.event_type == SLA_WARNING_RAISED]) == 1
    assert len(created) == 1


async def test_scan_warning_then_breach__timeline_shows_warning_then_breach() -> None:
    """End-to-end worker view: a clock that first crosses the
    warning window and then the deadline lands two timeline rows
    (one ``SlaWarningRaised``, one ``SlaBreached``) and fires both
    escalations.
    """
    clock_repo = InMemorySlaClockRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    creator, created = _make_task_creator()

    clock = _make_clock(
        warning_at=T0 + 42 * HOUR,
        due_at=T0 + 48 * HOUR,
        escalation_on_warning={
            "type": "create_task",
            "task_type": "supervisor_review",
            "queue": "supervisor_queue",
        },
        escalation_on_breach={
            "type": "create_task",
            "task_type": "escalation",
            "queue": "supervisor_queue",
        },
    )
    clock_repo.add(clock)

    # First tick: inside the warning window.
    warn_result = await scan_warning_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=FixedClock(now=T0 + 45 * HOUR),
    )
    assert warn_result.warned == [clock.id]
    assert warn_result.considered == 1

    # Second tick: past the deadline.
    breach_result = await scan_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=FixedClock(now=T0 + 49 * HOUR),
    )
    assert breach_result.breached == [clock.id]

    # The worker sees both rows in the timeline.
    assert len([e for e in audit.events if e.event_type == SLA_WARNING_RAISED]) == 1
    assert len([e for e in audit.events if e.event_type == SLA_BREACHED]) == 1
    # Both escalations fired — one for the warning, one for the breach.
    assert len(created) == 2
    types = {t.type for t in created}
    assert types == {"supervisor_review", "escalation"}


# ---------------------------------------------------------------------------
# 3. Non-breached clock stays active
# ---------------------------------------------------------------------------


async def test_scan_once__non_breached_clock_stays_active() -> None:
    """A counting clock well ahead of its deadline is left alone:
    no breach audit row, no outbox row, no escalation task.
    """
    clock_repo = InMemorySlaClockRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    creator, created = _make_task_creator()

    clock = _make_clock(
        due_at=T0 + 48 * HOUR,
        escalation_on_breach={
            "type": "create_task",
            "task_type": "escalation",
            "queue": "supervisor_queue",
        },
    )
    clock_repo.add(clock)

    # 24h in: still 24h to go.
    now = T0 + 24 * HOUR
    result = await scan_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=FixedClock(now=now),
    )

    assert result.breached == []
    assert result.considered == 0
    stored = await clock_repo.get(ACME_TENANT, clock.id)
    assert stored is not None
    assert stored.status is SlaClockStatus.ACTIVE
    assert audit.events == []
    assert outbox.records == []
    assert created == []


async def test_scan_warning_once__non_warning_clock_stays_active() -> None:
    """A counting clock ahead of the warning window is left alone:
    no warning audit row, no outbox row, no escalation task.
    """
    clock_repo = InMemorySlaClockRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    creator, created = _make_task_creator()

    clock = _make_clock(
        warning_at=T0 + 42 * HOUR,
        due_at=T0 + 48 * HOUR,
        escalation_on_warning={
            "type": "create_task",
            "task_type": "supervisor_review",
            "queue": "supervisor_queue",
        },
    )
    clock_repo.add(clock)

    # 24h in: still ahead of the warning window.
    now = T0 + 24 * HOUR
    result = await scan_warning_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=FixedClock(now=now),
    )

    assert result.warned == []
    stored = await clock_repo.get(ACME_TENANT, clock.id)
    assert stored is not None
    assert stored.status is SlaClockStatus.ACTIVE
    assert audit.events == []
    assert outbox.records == []
    assert created == []


# ---------------------------------------------------------------------------
# 4. Legal hold suppresses the escalation, not the audit row
# ---------------------------------------------------------------------------


async def test_scan_once__legal_hold_records_breach_but_skips_escalation() -> None:
    """FM-39 seam: a case under legal hold can still breach (the
    deadline did pass) but the configured escalation must not fire
    — disposition is blocked. The audit row still lands so the
    operator sees the breach in the timeline.
    """
    clock_repo = InMemorySlaClockRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    creator, created = _make_task_creator()

    clock = _make_clock(
        escalation_on_breach={
            "type": "create_task",
            "task_type": "escalation",
            "queue": "supervisor_queue",
        }
    )
    clock_repo.add(clock)

    now = clock.due_at + timedelta(minutes=1)
    result = await scan_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(held_cases={CASE_ID}),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=FixedClock(now=now),
    )

    assert result.breached == [clock.id]
    assert result.skipped_holds == 1
    assert created == []  # escalation suppressed

    row = next(e for e in audit.events if e.event_type == SLA_BREACHED)
    assert row.payload["case_under_legal_hold"] is True
    assert row.payload["escalation_fired"] is False


# ---------------------------------------------------------------------------
# 5. Tenant isolation
# ---------------------------------------------------------------------------


async def test_scan_once__tenant_isolation_does_not_breach_other_tenant_clock() -> None:
    """A clock in BETA_TENANT is not breached when the scanner runs
    for ACME_TENANT. The two-tenant test required by AGENTS.md
    §4.R5 / R12 for every repository method.
    """
    clock_repo = InMemorySlaClockRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    creator, _ = _make_task_creator()

    acme_clock = _make_clock(
        tenant_id=ACME_TENANT,
        due_at=T0 + 48 * HOUR,
        escalation_on_breach={
            "type": "create_task",
            "queue": "supervisor_queue",
        },
    )
    beta_clock = _make_clock(
        tenant_id=BETA_TENANT,
        clock_key="beta_review_due",
        due_at=T0 + 48 * HOUR,
        escalation_on_breach={
            "type": "create_task",
            "queue": "supervisor_queue",
        },
    )
    clock_repo.add(acme_clock)
    clock_repo.add(beta_clock)

    now = acme_clock.due_at + timedelta(minutes=1)
    result = await scan_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=FixedClock(now=now),
    )

    assert result.breached == [acme_clock.id]
    stored_beta = await clock_repo.get(BETA_TENANT, beta_clock.id)
    assert stored_beta is not None
    assert stored_beta.status is SlaClockStatus.ACTIVE
    breach_audit = [e for e in audit.events if e.event_type == SLA_BREACHED]
    assert len(breach_audit) == 1
    assert breach_audit[0].tenant_id == ACME_TENANT
    assert breach_audit[0].subject_id == acme_clock.id


# ---------------------------------------------------------------------------
# 6. Legacy ``breach_action`` field still fires the escalation
# ---------------------------------------------------------------------------


async def test_scan_once__legacy_breach_action_field_fires_escalation() -> None:
    """Backwards compatibility: clocks published under the H0
    schema carry the action in ``breach_action``; the scanner
    must still dispatch it so existing workflows keep working.
    """
    clock_repo = InMemorySlaClockRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutboxWriter()
    creator, created = _make_task_creator()

    clock = _make_clock(
        breach_action={
            "type": "create_task",
            "task_type": "legacy_escalation",
            "queue": "supervisor_queue",
        },
    )
    clock_repo.add(clock)

    now = clock.due_at + timedelta(minutes=1)
    await scan_once(
        tenant_id=ACME_TENANT,
        clock_repo=clock_repo,
        audit=audit,
        outbox=outbox,
        case_lookup=InMemoryCaseLookup(),
        task_creator=creator,
        task_repo=InMemoryTaskRepository(),
        clock=FixedClock(now=now),
    )

    assert len(created) == 1
    assert created[0].type == "legacy_escalation"
