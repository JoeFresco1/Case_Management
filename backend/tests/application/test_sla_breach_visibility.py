"""SLA breach visibility (FM-018): breach → task + timeline entry.

Naming: test_<unit>_<scenario>__<expectation>, given/when/then.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.application.sla import scan_sla
from app.domain.sla import SlaClock
from app.domain.task import Task


class _Clock:
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


async def test_scan_breach__fires_task_and_timeline_references_it() -> None:
    # given — an overdue clock with a create_task escalation
    tenant = uuid4()
    case_id = uuid4()
    now = datetime(2026, 1, 10, tzinfo=UTC)
    clock = SlaClock.start(
        tenant_id=tenant,
        case_id=case_id,
        clock_key="review_due",
        started_at=now - timedelta(hours=72),
        due_at=now - timedelta(hours=1),
        escalation_on_breach={
            "type": "create_task",
            "queue": "review_queue",
            "task_type": "escalation",
        },
    )
    repo = _ClockRepo([clock])
    audit = _Audit()
    outbox = _Outbox()
    tasks = _Tasks()
    # when
    result = await scan_sla.scan_once(
        tenant_id=tenant,
        clock_repo=repo,
        audit=audit,
        outbox=outbox,  # type: ignore[arg-type]
        case_lookup=_Cases(),  # type: ignore[arg-type]
        task_creator=tasks,
        task_repo=tasks,  # type: ignore[arg-type]
        clock=_Clock(now),
    )
    # then — task row exists
    assert result.breached == [clock.id]
    assert len(tasks.created) == 1
    assert tasks.created[0].type == "escalation"
    # then — timeline (audit) entry references the fired action
    assert len(audit.events) == 1
    payload = audit.events[0]["payload"]
    assert payload["clock_key"] == "review_due"
    assert payload["escalation_fired"] is True


async def test_scan_breach__notify_escalation_reports_no_action() -> None:
    # given — notify placeholder fires nothing but still audits
    tenant = uuid4()
    case_id = uuid4()
    now = datetime(2026, 1, 10, tzinfo=UTC)
    clock = SlaClock.start(
        tenant_id=tenant,
        case_id=case_id,
        clock_key="k",
        started_at=now - timedelta(hours=72),
        due_at=now - timedelta(hours=1),
        escalation_on_breach={"type": "notify", "queue": "q"},
    )
    repo = _ClockRepo([clock])
    audit = _Audit()
    outbox = _Outbox()
    tasks = _Tasks()
    # when
    await scan_sla.scan_once(
        tenant_id=tenant,
        clock_repo=repo,
        audit=audit,
        outbox=outbox,  # type: ignore[arg-type]
        case_lookup=_Cases(),  # type: ignore[arg-type]
        task_creator=tasks,
        task_repo=tasks,  # type: ignore[arg-type]
        clock=_Clock(now),
    )
    # then
    assert tasks.created == []
    assert audit.events[0]["payload"]["escalation_fired"] is False
