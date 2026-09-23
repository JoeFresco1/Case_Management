"""Tenant-listener enabled-path coverage (FM-002 / P11-US2 / app/db.py).

The ``do_orm_execute`` listener is opt-in defense-in-depth behind
``settings.tenant_listener_enabled`` plus an explicit per-session pin
(``set_current_tenant``). These tests pin the enabled path:

- an ORM select on an entity with ``tenant_id`` is filtered to the
  pinned tenant (and does not raise — the SQLAlchemy 2.x
  ``with_loader_criteria(entity, where_criteria)`` form is required;
  the old single-callable form raised ``TypeError`` on every
  tenant-pinned execution, surfacing as HTTP 500 on idempotent
  mutations whenever a prior test had booted the app with the
  listener enabled);
- Core DML with no ORM entity passes through untouched under a pin;
- an unpinned session is unfiltered (early-return path).

The mapped row lives on a ``test_``-prefixed table (same convention as
``tests/interfaces/http/test_idempotency_atomic.py::MUTATIONS``) so
migration/model parity checks ignore it. The listener registers on the
ORM ``Session`` class process-wide; leaving it installed is harmless —
it early-returns unless a session is pinned, and only the idempotency
provider pins sessions today.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, Integer, String, Uuid, insert, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.db import (
    Base,
    get_current_tenant,
    install_tenant_listener,
    set_current_tenant,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")


class _ListenerRow(Base):
    """Minimal tenant-scoped row for the enabled-path tests."""

    __tablename__ = "test_tenant_listener_rows"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Uuid, nullable=False)
    body = Column(String, nullable=False)


@pytest.fixture
async def listener_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Fresh sqlite schema carrying the ``test_`` listener table."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'listener.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose(close=True)


@pytest.fixture
def listener_session_factory(
    listener_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Session factory with the tenant listener installed and enabled."""
    install_tenant_listener(listener_engine, settings=Settings(tenant_listener_enabled=True))
    return async_sessionmaker(bind=listener_engine, expire_on_commit=False)


async def _seed_row(factory: async_sessionmaker[AsyncSession], tenant_id: UUID, body: str) -> None:
    """Seed one row for ``tenant_id`` without pinning (unfiltered write)."""
    async with factory() as session:
        await session.execute(
            insert(_ListenerRow).values(id=uuid4().int & 0x7FFFFFFF, tenant_id=tenant_id, body=body)
        )
        await session.commit()


async def test_listener_orm_select__pinned_session__sees_only_pinned_tenant(
    listener_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pinned session filters ORM selects to the pinned tenant."""
    # given: one row per tenant.
    await _seed_row(listener_session_factory, TENANT_A, "alpha")
    await _seed_row(listener_session_factory, TENANT_B, "beta")

    # when: the session is pinned to tenant A.
    async with listener_session_factory() as session:
        set_current_tenant(session, TENANT_A)
        assert get_current_tenant(session) == TENANT_A
        rows = (await session.execute(select(_ListenerRow))).scalars().all()

    # then: only tenant A's row is visible (no TypeError from the listener).
    assert {row.body for row in rows} == {"alpha"}


async def test_listener_core_dml__pinned_session__passes_through(
    listener_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Core DML with no ORM entity executes untouched under a pin."""
    # given: a session pinned to tenant A.
    async with listener_session_factory() as session:
        set_current_tenant(session, TENANT_A)

        # when: a Core insert with no ORM entity executes...
        await session.execute(
            insert(_ListenerRow).values(
                id=uuid4().int & 0x7FFFFFFF, tenant_id=TENANT_B, body="passthrough"
            )
        )
        await session.commit()

    # then: the row landed (the listener cannot filter entity-less DML).
    async with listener_session_factory() as session:
        bodies = [row.body for row in (await session.execute(select(_ListenerRow))).scalars().all()]
    assert "passthrough" in bodies


async def test_listener_orm_select__unpinned_session__returns_all_tenants(
    listener_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without a pin the listener early-returns and selects are unfiltered."""
    # given: one row per tenant.
    await _seed_row(listener_session_factory, TENANT_A, "alpha")
    await _seed_row(listener_session_factory, TENANT_B, "beta")

    # when: no tenant is pinned...
    async with listener_session_factory() as session:
        assert get_current_tenant(session) is None
        rows = (await session.execute(select(_ListenerRow))).scalars().all()

    # then: both rows are visible.
    assert {row.body for row in rows} == {"alpha", "beta"}


__all__: list[str] = []
