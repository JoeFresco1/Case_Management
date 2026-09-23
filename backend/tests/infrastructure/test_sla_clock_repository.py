"""Persistence regressions for the SLA warning-tier projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.sla import SlaClock
from app.infrastructure.persistence.repositories.sla_clock_repository import (
    SqlSlaClockRepository,
)


@pytest.mark.asyncio
async def test_sla_clock_sql_reload__warning_and_escalations_survive(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A worker restart must not lose warning timing or escalation actions."""
    started = datetime(2026, 9, 1, 12, tzinfo=UTC)
    clock = SlaClock.start(
        tenant_id=uuid4(),
        case_id=uuid4(),
        clock_key="review_due",
        started_at=started,
        due_at=started + timedelta(hours=48),
        warning_at=started + timedelta(hours=42),
        escalation_on_warning={"type": "create_task", "queue": "supervisor"},
        escalation_on_breach={"type": "notify", "queue": "supervisor"},
    )

    async with sql_session_factory() as session:
        await SqlSlaClockRepository(session).add(clock)
        await session.commit()

    async with sql_session_factory() as session:
        restored = await SqlSlaClockRepository(session).get(clock.tenant_id, clock.id)

    assert restored is not None
    assert restored.warning_at == clock.warning_at
    assert restored.escalation_on_warning == clock.escalation_on_warning
    assert restored.escalation_on_breach == clock.escalation_on_breach
