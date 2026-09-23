"""SQLite endpoint coverage for the admin governance dependency boundary."""

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
from app.infrastructure.persistence.admin_governance_http_dependencies import (
    SqlAdminGovernanceHttpDependencyProvider,
)
from app.infrastructure.persistence.repositories import SqlConfigurationPublicationRepository
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.admin_governance import router

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


async def _seed_publications(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Seed one publication row per tenant for the isolation assertion."""
    async with session_factory() as session:
        repository = SqlConfigurationPublicationRepository(session)
        for tenant_id, summary in (
            (TENANT_A, "Tenant A publication"),
            (TENANT_B, "Tenant B publication"),
        ):
            await repository.add(
                tenant_id=tenant_id,
                artifact_type="CaseTypeVersion",
                artifact_id=uuid4(),
                version_id=uuid4(),
                action="publish",
                actor_id=ADMIN_ID,
                change_summary=summary,
                occurred_at=NOW,
            )
        await session.commit()


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[FastAPI, dict[str, UUID]]:
    """Build a minimal app using the production composition provider."""
    current_tenant = {"id": TENANT_A}

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.state.admin_governance_http_dependencies = SqlAdminGovernanceHttpDependencyProvider()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=ADMIN_ID,
        tenant_id=current_tenant["id"],
        display_name="Synthetic Admin",
        roles=("process_admin",),
    )
    return app, current_tenant


def test_admin_governance_router__list_with_sqlite_provider__stays_tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The app-state provider preserves publication tenant isolation."""
    _run(_seed_publications(sql_session_factory))
    app, current_tenant = _build_app(sql_session_factory)

    with TestClient(app) as client:
        tenant_a_response = client.get("/api/v1/admin/publications")
        current_tenant["id"] = TENANT_B
        tenant_b_response = client.get("/api/v1/admin/publications")

    assert tenant_a_response.status_code == 200, tenant_a_response.text
    assert tenant_b_response.status_code == 200, tenant_b_response.text
    assert [item["change_summary"] for item in tenant_a_response.json()["items"]] == [
        "Tenant A publication"
    ]
    assert [item["change_summary"] for item in tenant_b_response.json()["items"]] == [
        "Tenant B publication"
    ]
