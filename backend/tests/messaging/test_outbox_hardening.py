"""Outbox hardening: safe errors, tenant scope, idempotent replay (FM-052/055).

Covers the G-task acceptance slice on top of ``test_outbox.py``:

- replay idempotency (reset once → ``False`` thereafter, then dispatch);
- poison dead-letter without secrets (subscriber raising a secret-bearing
  error is sanitized on the row);
- tenant scope (``count_lag`` / ``list_pending`` filter by tenant);
- safe errors (``sanitize_error`` redacts ``key=value`` secrets, truncates).

Layout follows AGENTS.md R5: ``test_<unit>_<scenario>__<expectation>``
with ``// given / // when / // then``. Sqlite-backed so no Docker is
needed (AGENTS.md R1 fallback).
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
from app.infrastructure.messaging.local_message_bus import LocalInProcessMessageBus
from app.infrastructure.messaging.outbox_dispatcher import (
    MAX_ATTEMPTS,
    OutboxDispatcher,
    _rebuild_payload,
    sanitize_error,
)
from app.infrastructure.persistence import models  # noqa: F401 — register tables
from app.infrastructure.persistence.models import Outbox as OutboxRow


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


def _case_payload(tenant_id: UUID) -> dict[str, Any]:
    return {
        "case_id": str(uuid4()),
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
) -> UUID:
    tenant = tenant_id or uuid4()
    async with sm() as session:
        row = OutboxRow(
            tenant_id=tenant,
            event_type=event_type,
            event_id=uuid4(),
            payload=_rebuild_payload(
                wire_payload or _case_payload(tenant),
                correlation_id=None,
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
    async with sm() as session:
        await session.execute(
            OutboxRow.__table__.update()  # type: ignore[attr-defined]
            .where(OutboxRow.id == row_id)
            .values(next_attempt_at=None)
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Safe errors
# ---------------------------------------------------------------------------


def test_sanitize_error__secret_value__redacted() -> None:
    # // given — a subscriber error echoing a secret key=value pair
    raw = "RuntimeError('downstream rejected password=hunter2 for user')"
    # // when
    safe = sanitize_error(raw)
    # // then — the value is gone but the triage context survives
    assert "hunter2" not in safe
    assert "[REDACTED]" in safe
    assert "downstream rejected" in safe


def test_sanitize_error__long_input__truncated() -> None:
    # // given / when
    safe = sanitize_error("x" * 10_000)
    # // then — portable TEXT column cap holds
    assert len(safe) <= 500


@pytest.mark.asyncio
async def test_outbox__poison_error_with_secret__dead_lettered_without_secret(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # // given — a subscriber whose failure text contains a secret
    tenant_id = uuid4()
    bus = LocalInProcessMessageBus()

    async def boom(_event: dict[str, Any]) -> None:
        raise RuntimeError("downstream rejected api_key=SUPERSECRET123")

    await bus.subscribe(boom)
    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    row_id = await _enqueue(sqlite_sessionmaker, tenant_id=tenant_id)

    # // when — drain to the dead-letter cap, jumping the backoff gate
    for _ in range(MAX_ATTEMPTS):
        await dispatcher.drain_once()
        await _force_due(sqlite_sessionmaker, row_id)

    # // then — dead-lettered, and the stored error carries no secret
    row = await _get_row(sqlite_sessionmaker, row_id)
    assert row.attempt_count >= MAX_ATTEMPTS
    assert row.dispatched_at is None
    assert row.last_error is not None
    assert "SUPERSECRET123" not in row.last_error
    assert "[REDACTED]" in row.last_error


# ---------------------------------------------------------------------------
# Tenant scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox__count_lag__tenant_scoped(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # // given — one pending row per tenant
    tenant_a, tenant_b = uuid4(), uuid4()
    bus = LocalInProcessMessageBus()
    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    await _enqueue(sqlite_sessionmaker, tenant_id=tenant_a)
    await _enqueue(sqlite_sessionmaker, tenant_id=tenant_b)

    # // when / then — each tenant sees only their own lag
    lag_a = await dispatcher.count_lag(tenant_id=tenant_a)
    lag_b = await dispatcher.count_lag(tenant_id=tenant_b)
    assert lag_a["pending"] == 1
    assert lag_a["total"] == 1
    assert lag_b["pending"] == 1
    assert lag_b["total"] == 1
    # // and — the global console still sees both
    lag_all = await dispatcher.count_lag()
    assert lag_all["pending"] == 2


@pytest.mark.asyncio
async def test_outbox__list_pending__tenant_scoped(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # // given — one pending row per tenant
    tenant_a, tenant_b = uuid4(), uuid4()
    bus = LocalInProcessMessageBus()
    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    await _enqueue(sqlite_sessionmaker, tenant_id=tenant_a)
    await _enqueue(sqlite_sessionmaker, tenant_id=tenant_b)

    # // when
    rows_a = await dispatcher.list_pending(tenant_id=tenant_a)
    rows_b = await dispatcher.list_pending(tenant_id=tenant_b)

    # // then — no cross-tenant enumeration of failed work
    assert [r.tenant_id for r in rows_a] == [tenant_a]
    assert [r.tenant_id for r in rows_b] == [tenant_b]


# ---------------------------------------------------------------------------
# Safe replay (idempotent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox__replay__idempotent_second_call_noop(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # // given — a dead-lettered row with a recovered subscriber
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

    # // when — operator replays twice before the next tick
    first = await dispatcher.replay(row_id)
    second = await dispatcher.replay(row_id)

    # // then — first resets, second is a no-op (no duplicate work queued)
    assert first is True
    assert second is False
    row = await _get_row(sqlite_sessionmaker, row_id)
    assert row.attempt_count == 0
    assert row.next_attempt_at is None
    # failure history preserved for the operator
    assert row.last_error == "stuck"

    # // when — the tick runs once
    assert await dispatcher.drain_once() == 1

    # // then — exactly one delivery with the stable event_id
    assert len(delivered) == 1
    row = await _get_row(sqlite_sessionmaker, row_id)
    assert row.dispatched_at is not None
    assert delivered[0]["event_id"] == str(row.event_id)


@pytest.mark.asyncio
async def test_outbox__replay__fresh_row__noop(
    sqlite_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # // given — a fresh pending row that needs no replay
    bus = LocalInProcessMessageBus()
    dispatcher = OutboxDispatcher(sessionmaker=sqlite_sessionmaker, bus=bus, sleep_seconds=0.01)
    row_id = await _enqueue(sqlite_sessionmaker)

    # // when / then — replay is a no-op, the row stays eligible
    assert await dispatcher.replay(row_id) is False
    row = await _get_row(sqlite_sessionmaker, row_id)
    assert row.attempt_count == 0
    assert row.dispatched_at is None
