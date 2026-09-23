"""SQLite endpoint coverage for the admin role dependency boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import DomainError
from app.domain.role import Role
from app.infrastructure.persistence.admin_roles_http_dependencies import (
    SqlAdminRolesHttpDependencyProvider,
)
from app.infrastructure.persistence.repositories import SqlRoleRepository
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.admin_roles import router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _run(coro: Any) -> Any:
    """Run an async seed operation for the synchronous TestClient test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_roles(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Seed one role per tenant so the endpoint proves tenant scoping."""
    async with session_factory() as session:
        repository = SqlRoleRepository(session)
        await repository.add(
            Role(
                id=uuid4(),
                tenant_id=TENANT_A,
                name="tenant-a-role",
                description="Synthetic tenant A role",
                permissions=frozenset({"case.read"}),
                created_at=NOW,
            )
        )
        await repository.add(
            Role(
                id=uuid4(),
                tenant_id=TENANT_B,
                name="tenant-b-role",
                description="Synthetic tenant B role",
                permissions=frozenset({"case.read"}),
                created_at=NOW,
            )
        )
        await session.commit()


def _build_app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    """Build a minimal app using the production provider composition."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.admin_roles_http_dependencies = SqlAdminRolesHttpDependencyProvider()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=ADMIN_ID,
        tenant_id=TENANT_A,
        display_name="Synthetic Admin",
        roles=("process_admin",),
    )
    return app


def test_admin_roles_router__list_with_sqlite_provider__stays_tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The router resolves the app-state provider and hides another tenant."""
    _run(_seed_roles(sql_session_factory))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        response = client.get("/api/v1/admin/roles")

    assert response.status_code == 200, response.text
    assert [role["name"] for role in response.json()] == ["tenant-a-role"]
