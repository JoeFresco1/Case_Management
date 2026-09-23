"""Persistence regressions for tenant-scoped idempotency keys (T022)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.persistence import models  # noqa: F401
from app.infrastructure.persistence.audit_outbox import SqlIdempotencyStore


async def test_idempotency_store__same_key_in_two_tenants__persists_both(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A client may safely reuse a key in another tenant."""
    tenant_a = UUID("11111111-1111-1111-1111-111111111111")
    tenant_b = UUID("22222222-2222-2222-2222-222222222222")
    now = datetime.now(UTC)

    async with sql_session_factory() as session:
        session.add_all(
            [
                models.Tenant(id=tenant_a, key="A", name="Tenant A", created_at=now),
                models.Tenant(id=tenant_b, key="B", name="Tenant B", created_at=now),
            ]
        )
        await session.flush()
        store = SqlIdempotencyStore(session)
        await store.put(
            tenant_id=tenant_a,
            key="same-key",
            request_hash="a" * 64,
            response_status=201,
            response_body={"tenant": "a"},
            correlation_id="corr-a",
        )
        await store.put(
            tenant_id=tenant_b,
            key="same-key",
            request_hash="b" * 64,
            response_status=201,
            response_body={"tenant": "b"},
            correlation_id="corr-b",
        )
        await session.commit()

        response_a = await store.get(tenant_id=tenant_a, key="same-key")
        response_b = await store.get(tenant_id=tenant_b, key="same-key")
        assert response_a is not None
        assert response_b is not None
        assert response_a["response_body"] == {"tenant": "a"}
        assert response_b["response_body"] == {"tenant": "b"}


async def test_idempotency_key_model__primary_key__is_tenant_and_key(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ORM contract matches the composite migration contract."""
    async with sql_session_factory() as session:
        connection = await session.connection()
        primary_key = await connection.run_sync(
            lambda connection: inspect(connection).get_pk_constraint("idempotency_keys")
        )
    assert set(primary_key["constrained_columns"]) == {"tenant_id", "key"}
