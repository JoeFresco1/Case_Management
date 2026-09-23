"""Outbox dispatcher tests (FM-052 / P13-US9 / P12-US4).

Pinned behaviors:

1. **Retry counter increments on failure** — a subscriber that raises
   bumps ``attempt_count`` and records ``last_error``; the row stays
   pending so the next ``tick`` re-attempts.
2. **Dead-letter after ``MAX_ATTEMPTS``** — after ``MAX_ATTEMPTS``
   failed drains, the row remains in the table (not lost) with
   ``attempt_count`` at the cap; ``count_lag`` reports it under
   ``dead_lettered``.
3. **Replay succeeds** — ``replay`` resets a row's ``attempt_count`` so
   the next ``tick`` dispatches it; the row stays durable the whole
   time.
4. **Poison message not lost** — an ``event_type`` that is not in the
   catalog is dead-lettered with a stable ``last_error`` rather than
   silently dropped; an operator can replay it once the catalog entry
   is published.

Test layout follows AGENTS.md §4.R5 — ``test_<unit>_<scenario>__<expectation>``,
``// given / // when / // then`` per test. The fixture layer is
sqlite-backed so the suite never requires PostgreSQL (AGENTS.md §4.R1
fallback); ``Base.metadata.create_all`` builds the schema in-test.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db import Base
from app.domain import events as catalog
from app.infrastructure.messaging.local_message_bus import LocalInProcessMessageBus
from app.infrastructure.messaging.outbox_dispatcher import (
    MAX_ATTEMPTS,
    OutboxDispatcher,
    _rebuild_payload,
    backoff_delay_seconds,
)
from app.infrastructure.persistence import models  # noqa: F401 — register tables
from app.infrastructure.persistence.models import Outbox as OutboxRow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_sessionmaker() -> Iterator[async_sessionmaker[AsyncSession]]:
    """Per-test sqlite URL. No shared state between tests."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def _init() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    try:
        yield sm
    finally:
        asyncio.run(engine.dispose())
        with contextlib.suppress(OSError):
            os.remove(path)


def _case_payload(tenant_id: UUID, *, case_id: UUID | None = None) -> dict[str, Any]:
    return {
        "case_id": str(case_id or uuid4()),
        "reference": "C-ACME-2026-000001",
        "case_type_key": "ServiceRequest",
        "tenant_id": str(tenant_id),
        "actor_id": str(uuid4()),
        "occurred_at": "2026-09-02T00:00:00Z",
    }


async def _enqueue(
    sm: async_sessionmaker[AsyncSession],
    *,
    event_type: str = "CaseCreated",
    tenant_id: UUID | None = None,
    wire_payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> UUID:
    tenant = tenant_id or uuid4()
    async with sm() as session:
        row = OutboxRow(
            tenant_id=tenant,
            event_type=event_type,
            event_id=uuid4(),
            payload=_rebuild_payload(
                wire_payload or _case_payload(tenant),
                correlation_id=correlation_id,
                causation_id=None,
                occurred_at=None,
            ),
            created_at=datetime.now(tz=UTC),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _get_row(sm: async_sessionmaker[AsyncSession], row_id: UUID) -> OutboxRow:
    async with sm() as session:
        return (await session.execute(select(OutboxRow).where(OutboxRow.id == row_id))).scalar_one()


async def _force_due(sm: async_sessionmaker[AsyncSession], row_id: UUID) -> None:
    """Clear a row's backoff gate so the next drain re-attempts it.

    Production ticks wait out ``next_attempt_at`` in real time; tests
    jump the gate directly to assert the retry *counting* without
    sleeping through exponential delays.
    """
    async with sm() as session:
        await session.execute(
            OutboxRow.__table__.update()  # type: ignore[attr-defined]
            .where(OutboxRow.id == row_id)
            .values(next_attempt_at=None)
        )
        await session.commit()


# ---------------------------------------------------------------------------
# 1. Retry counter increments on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox__failed_publish_increments_attempt_count_and_records_error(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # given — a fresh outbox row and a subscriber that always raises.
    tenant_id = uuid4()
    bus = LocalInProcessMessageBus()

    async def boom(_event: dict[str, Any]) -> None:
        raise RuntimeError("downstream unavailable")

    await bus.subscribe(boom)

    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    row_id = await _enqueue(sqlite_sessionmaker, tenant_id=tenant_id)

    # when
    await dispatcher.drain_once()

    # then — the row is NOT lost; ``attempt_count`` was incremented and
    # ``last_error`` carries the truncated cause. ``dispatched_at`` stays
    # None so the next tick retries.
    row = await _get_row(sqlite_sessionmaker, row_id)
    assert row.dispatched_at is None
    assert row.attempt_count == 1
    assert row.last_error is not None
    assert "downstream unavailable" in row.last_error


@pytest.mark.asyncio
async def test_outbox__repeated_failures_increment_monotonically(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # given — three successive ``drain_once`` calls against a bus whose
    # subscriber always raises.
    tenant_id = uuid4()
    bus = LocalInProcessMessageBus()

    async def boom(_event: dict[str, Any]) -> None:
        raise RuntimeError("persistent failure")

    await bus.subscribe(boom)

    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    row_id = await _enqueue(sqlite_sessionmaker, tenant_id=tenant_id)

    # when
    for expected in (1, 2, 3):
        await dispatcher.drain_once()
        row = await _get_row(sqlite_sessionmaker, row_id)
        # then — counter advances by exactly one per drain.
        assert row.attempt_count == expected
        assert row.last_error is not None
        assert "persistent failure" in row.last_error
        assert row.dispatched_at is None
        # Jump the backoff gate: the next drain must re-attempt
        # immediately in-test (production would wait out the delay).
        await _force_due(sqlite_sessionmaker, row_id)


# ---------------------------------------------------------------------------
# 2. Dead-letter after MAX_ATTEMPTS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox__dead_letters_after_max_attempts(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # given — a row + a subscriber that always fails.
    tenant_id = uuid4()
    bus = LocalInProcessMessageBus()

    async def boom(_event: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    await bus.subscribe(boom)

    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    row_id = await _enqueue(sqlite_sessionmaker, tenant_id=tenant_id)

    # when — drain ``MAX_ATTEMPTS`` times so the counter hits the cap,
    # jumping the backoff gate between drains (see helper).
    for _ in range(MAX_ATTEMPTS):
        await dispatcher.drain_once()
        await _force_due(sqlite_sessionmaker, row_id)

    # then — the row stays in the table (not lost), ``attempt_count`` is
    # at the cap, ``last_error`` is recorded, ``dispatched_at`` is None,
    # and ``is_dead_lettered`` (derived) is True.
    row = await _get_row(sqlite_sessionmaker, row_id)
    assert row.attempt_count >= MAX_ATTEMPTS
    assert row.dispatched_at is None
    assert row.last_error is not None
    assert row.attempt_count >= MAX_ATTEMPTS

    lag = await dispatcher.count_lag()
    assert lag["dead_lettered"] == 1
    assert lag["retrying"] == 0
    assert lag["pending"] == 1


@pytest.mark.asyncio
async def test_outbox__dead_lettered_row_is_skipped_by_subsequent_ticks(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # given — a row already dead-lettered by ``MAX_ATTEMPTS`` failures
    # and a subscriber that has now recovered.
    tenant_id = uuid4()
    bus = LocalInProcessMessageBus()
    delivered: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        delivered.append(event)

    await bus.subscribe(on_event)

    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    row_id = await _enqueue(sqlite_sessionmaker, tenant_id=tenant_id)

    async with sqlite_sessionmaker() as session:
        await session.execute(
            OutboxRow.__table__.update()  # type: ignore[attr-defined]
            .where(OutboxRow.id == row_id)
            .values(attempt_count=MAX_ATTEMPTS, last_error="seeded dead-letter")
        )
        await session.commit()

    # when — drain again with the recovered subscriber.
    await dispatcher.drain_once()

    # then — the dead-lettered row was skipped (still dispatched_at None,
    # attempt_count at cap) and the bus received nothing. The dispatcher
    # never silently re-attempts a row the cap said to abandon.
    row = await _get_row(sqlite_sessionmaker, row_id)
    assert row.dispatched_at is None
    assert row.attempt_count == MAX_ATTEMPTS
    assert row.last_error == "seeded dead-letter"
    assert delivered == []


# ---------------------------------------------------------------------------
# 3. Replay succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox__replay_resets_attempt_count_and_dispatch_succeeds(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # given — a dead-lettered row + a subscriber that now succeeds.
    tenant_id = uuid4()
    bus = LocalInProcessMessageBus()
    delivered: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        delivered.append(event)

    await bus.subscribe(on_event)

    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    row_id = await _enqueue(sqlite_sessionmaker, tenant_id=tenant_id)

    async with sqlite_sessionmaker() as session:
        await session.execute(
            OutboxRow.__table__.update()  # type: ignore[attr-defined]
            .where(OutboxRow.id == row_id)
            .values(attempt_count=MAX_ATTEMPTS, last_error="stuck")
        )
        await session.commit()

    # when — operator triggers replay, then a tick runs.
    reset = await dispatcher.replay(row_id)
    await dispatcher.drain_once()

    # then — the row was reset, dispatched, and delivered. ``last_error``
    # was preserved at "stuck" until dispatch succeeded (the dispatch
    # path clears it on success).
    assert reset is True
    row = await _get_row(sqlite_sessionmaker, row_id)
    assert row.attempt_count == 1  # reset to 0, then one successful publish
    assert row.dispatched_at is not None
    assert row.last_error is None
    assert len(delivered) == 1
    assert delivered[0]["event_type"] == "CaseCreated"


@pytest.mark.asyncio
async def test_outbox__replay_returns_false_for_dispatched_or_missing(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # given — a row that is already dispatched, plus a random uuid.
    bus = LocalInProcessMessageBus()
    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    row_id = await _enqueue(sqlite_sessionmaker)

    async with sqlite_sessionmaker() as session:
        await session.execute(
            OutboxRow.__table__.update()  # type: ignore[attr-defined]
            .where(OutboxRow.id == row_id)
            .values(dispatched_at=datetime.now(tz=UTC))
        )
        await session.commit()

    # when / then — replay on a dispatched row returns False (no-op).
    assert await dispatcher.replay(row_id) is False
    # and replay on a non-existent row also returns False.
    assert await dispatcher.replay(uuid4()) is False


# ---------------------------------------------------------------------------
# 4. Poison message not lost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox__unknown_event_type_is_dead_lettered_not_lost(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # given — a stray row carrying an event type that is not in the
    # catalog. The dispatcher refuses to publish, the row stays in the
    # table, and the operator can see why via ``last_error``.
    tenant_id = uuid4()
    bus = LocalInProcessMessageBus()
    received: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        received.append(event)

    await bus.subscribe(on_event)

    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    row_id = await _enqueue(
        sqlite_sessionmaker,
        tenant_id=tenant_id,
        event_type="SomethingRemoved",
        wire_payload={"tenant_id": str(tenant_id)},
    )

    # when
    await dispatcher.drain_once()

    # then — the row is NOT deleted and NOT published. The subscriber
    # received nothing.
    row = await _get_row(sqlite_sessionmaker, row_id)
    assert row.dispatched_at is None
    assert row.attempt_count >= MAX_ATTEMPTS
    assert row.last_error == "unknown_event_type"
    assert received == []

    # and — once the catalog entry is added, an operator replay
    # followed by a drain dispatches the row successfully.
    original_catalog = catalog.EVENT_CATALOG
    try:
        catalog.EVENT_CATALOG = catalog.EVENT_CATALOG + (
            catalog.EventTypeSpec(
                event_type="SomethingRemoved",
                version=1,
                description="recovered poison row",
                payload_keys=("tenant_id",),
            ),
        )
        reset = await dispatcher.replay(row_id)
        await dispatcher.drain_once()
        assert reset is True
        row = await _get_row(sqlite_sessionmaker, row_id)
        assert row.dispatched_at is not None
        assert received == [
            {
                "event_type": "SomethingRemoved",
                "event_id": str(row.event_id),
                "tenant_id": str(tenant_id),
                "correlation_id": None,
                "causation_id": None,
                "occurred_at": None,
                "version": 1,
                "payload": {"tenant_id": str(tenant_id)},
            }
        ]
    finally:
        catalog.EVENT_CATALOG = original_catalog


# ---------------------------------------------------------------------------
# Lag summary shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox__count_lag_reports_total_pending_dead_and_retrying(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # given — three rows in distinct states: dispatched, retrying, and
    # dead-lettered.
    tenant_id = uuid4()
    bus = LocalInProcessMessageBus()

    async def on_event(event: dict[str, Any]) -> None:
        return None

    await bus.subscribe(on_event)

    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)

    dispatched_row_id = await _enqueue(sqlite_sessionmaker, tenant_id=tenant_id)
    retrying_row_id = await _enqueue(sqlite_sessionmaker, tenant_id=tenant_id)
    dead_row_id = await _enqueue(sqlite_sessionmaker, tenant_id=tenant_id)

    async with sqlite_sessionmaker() as session:
        await session.execute(
            OutboxRow.__table__.update()  # type: ignore[attr-defined]
            .where(OutboxRow.id == dispatched_row_id)
            .values(dispatched_at=datetime.now(tz=UTC))
        )
        await session.execute(
            OutboxRow.__table__.update()  # type: ignore[attr-defined]
            .where(OutboxRow.id == retrying_row_id)
            .values(attempt_count=2, last_error="flaky")
        )
        await session.execute(
            OutboxRow.__table__.update()  # type: ignore[attr-defined]
            .where(OutboxRow.id == dead_row_id)
            .values(attempt_count=MAX_ATTEMPTS, last_error="stuck")
        )
        await session.commit()

    # when
    lag = await dispatcher.count_lag()

    # then — every counter agrees with the seeded state.
    assert lag == {
        "total": 3,
        "pending": 2,
        "dead_lettered": 1,
        "retrying": 1,
    }


# ---------------------------------------------------------------------------
# Retry backoff (T029)
# ---------------------------------------------------------------------------


def test_backoff_delay_seconds__doubles_with_cap() -> None:
    """The schedule is deterministic: 5, 10, 20, 40 … capped at 300."""
    assert backoff_delay_seconds(1) == 5.0
    assert backoff_delay_seconds(2) == 10.0
    assert backoff_delay_seconds(3) == 20.0
    assert backoff_delay_seconds(4) == 40.0
    assert backoff_delay_seconds(10) == 300.0
    assert backoff_delay_seconds(100) == 300.0


@pytest.mark.asyncio
async def test_outbox__failed_publish__sets_next_attempt_at(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A failure arms the backoff gate instead of hot-spinning."""
    # // given
    bus = LocalInProcessMessageBus()

    async def boom(_event: dict[str, Any]) -> None:
        raise RuntimeError("downstream unavailable")

    await bus.subscribe(boom)
    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    row_id = await _enqueue(sqlite_sessionmaker)

    # // when
    before = datetime.now(tz=UTC)
    await dispatcher.drain_once()

    # // then — next attempt is ~5s out (first-failure delay).
    # NOTE: sqlite returns naive datetimes; re-anchor before comparing.
    row = await _get_row(sqlite_sessionmaker, row_id)
    assert row.attempt_count == 1
    assert row.next_attempt_at is not None
    due = row.next_attempt_at.replace(tzinfo=UTC)
    assert due >= before
    assert (due - before).total_seconds() <= 30.0


@pytest.mark.asyncio
async def test_outbox__not_due_row__skipped_by_drain(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A row inside its backoff window is not re-attempted."""
    # // given — a failed row (gate armed) and a recovered subscriber.
    bus = LocalInProcessMessageBus()
    delivered: list[dict[str, Any]] = []

    async def on_event(event: dict[str, Any]) -> None:
        delivered.append(event)

    await bus.subscribe(on_event)
    fail_bus = LocalInProcessMessageBus()

    async def boom(_event: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    await fail_bus.subscribe(boom)
    failing = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=fail_bus, sleep_seconds=0.01)
    row_id = await _enqueue(sqlite_sessionmaker)
    await failing.drain_once()
    assert (await _get_row(sqlite_sessionmaker, row_id)).next_attempt_at is not None

    # // when — drain with the recovered bus while the gate holds.
    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    dispatched = await dispatcher.drain_once()

    # // then — skipped: nothing delivered, counter untouched.
    assert dispatched == 0
    assert delivered == []
    assert (await _get_row(sqlite_sessionmaker, row_id)).attempt_count == 1

    # // when — the gate passes (jumped in-test), the row dispatches.
    await _force_due(sqlite_sessionmaker, row_id)
    assert await dispatcher.drain_once() == 1

    # // then
    assert len(delivered) == 1
    assert (await _get_row(sqlite_sessionmaker, row_id)).next_attempt_at is None


@pytest.mark.asyncio
async def test_outbox__replay__clears_next_attempt_at(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Operator replay resets the backoff gate along with the counter."""
    # // given — a failed row with an armed gate.
    bus = LocalInProcessMessageBus()

    async def boom(_event: dict[str, Any]) -> None:
        raise RuntimeError("boom")

    await bus.subscribe(boom)
    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    row_id = await _enqueue(sqlite_sessionmaker)
    await dispatcher.drain_once()
    assert (await _get_row(sqlite_sessionmaker, row_id)).next_attempt_at is not None

    # // when
    assert await dispatcher.replay(row_id) is True

    # // then
    row = await _get_row(sqlite_sessionmaker, row_id)
    assert row.attempt_count == 0
    assert row.next_attempt_at is None
