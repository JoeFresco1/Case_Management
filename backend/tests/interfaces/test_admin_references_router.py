"""SQLite endpoint coverage for the admin reference-format dependency boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import UserContext, get_clock, get_current_user, get_db
from app.domain.errors import DomainError
from app.infrastructure.persistence.admin_references_http_dependencies import (
    SqlAdminReferencesHttpDependencyProvider,
)
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.admin_references import router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _run(coro: Any) -> Any:
    """Run an async setup operation for the synchronous TestClient test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Any,
) -> FastAPI:
    """Build a minimal app using the production composition provider."""

    current_tenant = TENANT_A

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.state.admin_references_http_dependencies = SqlAdminReferencesHttpDependencyProvider()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=ADMIN_ID,
        tenant_id=current_tenant,
        display_name="Synthetic Admin",
        roles=("process_admin",),
    )
    return app


def test_admin_references_router__configured_tenant_isolation__hides_override(
    sql_session_factory: async_sessionmaker[AsyncSession],
    clock: Any,
) -> None:
    """A configured format for tenant A is not returned for tenant B."""
    app = _build_app(sql_session_factory, clock)

    with TestClient(app) as client:
        configure_response = client.post(
            "/api/v1/admin/reference-format",
            json={"template": "A-{SEQ}"},
        )
        assert configure_response.status_code == 200, configure_response.text

        app.dependency_overrides[get_current_user] = lambda: UserContext(
            user_id=ADMIN_ID,
            tenant_id=TENANT_B,
            display_name="Synthetic Admin",
            roles=("process_admin",),
        )
        tenant_b_response = client.get("/api/v1/admin/reference-format")

        app.dependency_overrides[get_current_user] = lambda: UserContext(
            user_id=ADMIN_ID,
            tenant_id=TENANT_A,
            display_name="Synthetic Admin",
            roles=("process_admin",),
        )
        tenant_a_response = client.get("/api/v1/admin/reference-format")

    assert tenant_b_response.status_code == 200, tenant_b_response.text
    assert tenant_b_response.json()["is_default"] is True
    assert tenant_b_response.json()["template"] == "C-{TENANT}-{YYYY}-{SEQ}"
    assert tenant_a_response.status_code == 200, tenant_a_response.text
    assert tenant_a_response.json()["is_default"] is False
    assert tenant_a_response.json()["template"] == "A-{SEQ}"


__all__ = []
