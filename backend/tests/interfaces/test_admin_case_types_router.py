"""SQLite-backed checks for the admin CaseType HTTP composition boundary."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.deps import UserContext, get_clock, get_current_user, get_db
from app.domain.case_type import CaseType
from app.domain.errors import DomainError
from app.infrastructure.persistence.admin_case_types_http_dependencies import (
    SqlAdminCaseTypesHttpDependencyProvider,
)
from app.infrastructure.persistence.repositories import SqlCaseTypeRepository
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.admin_case_types import router
from tests._live_roles import seed_live_roles

TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
ADMIN_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _run(coro: Any) -> Any:
    """Run an async seed operation for the synchronous TestClient test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _seed_case_type(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed the CaseType header required by the draft endpoint."""
    async with session_factory() as session:
        case_type = CaseType.create(
            tenant_id=TENANT_ID,
            key="ServiceRequest",
            name="Service Request",
        )
        await SqlCaseTypeRepository(session).add(case_type)
        await session.commit()


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Any,
) -> FastAPI:
    """Build a minimal app with the production provider composition."""
    _run(
        seed_live_roles(
            session_factory,
            tenant_id=TENANT_ID,
            user_id=ADMIN_ID,
            role_names=("process_admin",),
        )
    )

    async def _override_get_db() -> Any:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.state.admin_case_types_http_dependencies = SqlAdminCaseTypesHttpDependencyProvider()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_clock] = lambda: clock
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=ADMIN_ID,
        tenant_id=TENANT_ID,
        display_name="Synthetic Admin",
        roles=("process_admin",),
    )
    app.dependency_overrides[__import__("app.config", fromlist=["get_settings"]).get_settings] = (
        lambda: Settings(app_env="local")
    )
    return app


def test_admin_case_types_router__draft_with_sqlite_provider__creates_version(
    sql_session_factory: async_sessionmaker[AsyncSession],
    clock: Any,
) -> None:
    """The router consumes its app-state provider and preserves draft behavior."""
    _run(_seed_case_type(sql_session_factory))
    app = _build_app(sql_session_factory, clock)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/case-types/ServiceRequest/versions",
            json={
                "schema": {
                    "fields": [
                        {
                            "key": "title",
                            "label": "Title",
                            "type": "text",
                            "required": True,
                        }
                    ],
                    "document_classifications": [],
                    "rules": [],
                }
            },
        )
        assert response.status_code == 201, response.text
        version_id = response.json()["case_type_version_id"]

        publish_response = client.post(
            f"/api/v1/admin/case-types/ServiceRequest/versions/{version_id}/publish"
        )
        history_response = client.get("/api/v1/admin/case-types/ServiceRequest/versions")

    assert response.json()["version_number"] == 1
    assert response.json()["status"] == "draft"
    assert publish_response.status_code == 200, publish_response.text
    assert publish_response.json()["status"] == "published"
    assert history_response.status_code == 200, history_response.text
    assert history_response.json()["versions"][0]["status"] == "published"
