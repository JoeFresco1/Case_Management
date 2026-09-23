"""Enforcement coverage for break-glass overrides (FM-035 / P11-US10 / P06-US9).

Proves the shippable outcome end-to-end against SQLite: an auditor in
tenant A holding an active grant minted by tenant B's administrator
can read a tenant-B case and search tenant-B cases; every action is
audit-flagged with the grant id, scope, and reason; expired, revoked,
or missing grants deny without leaking cross-tenant existence.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.security import break_glass
from app.deps import UserContext, get_clock, get_current_user, get_db
from app.domain.break_glass import BreakGlassGrant, BreakGlassScope
from app.domain.case import Case, CaseReference, CaseStatus
from app.infrastructure.persistence.case_http_dependencies import (
    SqlCaseHttpDependencyProvider,
)
from app.infrastructure.persistence.models import AuditEvent as AuditEventRow
from app.infrastructure.persistence.models import Tenant as TenantRow
from app.infrastructure.persistence.repositories import (
    SqlBreakGlassGrantRepository,
    SqlCaseRepository,
)
from app.infrastructure.persistence.search_http_dependencies import (
    SqlSearchHttpDependencyProvider,
)
from app.infrastructure.persistence.security_http_dependencies import (
    SqlSecurityHttpDependencyProvider,
)
from app.interfaces.http.routers.security import router as security_router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
AUDITOR_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_B_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _run(coro: Any) -> Any:
    """Run an async setup step from a sync test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FrozenClock:
    """Deterministic clock pinned to the module ``NOW``."""

    def now(self) -> datetime:
        return NOW


def _auditor() -> UserContext:
    return UserContext(
        user_id=AUDITOR_ID,
        tenant_id=TENANT_A,
        display_name="Auditor",
        roles=("auditor",),
    )


def _admin_b() -> UserContext:
    return UserContext(
        user_id=ADMIN_B_ID,
        tenant_id=TENANT_B,
        display_name="Tenant B Admin",
        roles=("process_admin",),
    )


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    holder: dict[str, UserContext],
) -> FastAPI:
    """Build the security surface on the production SQL providers."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.security_http_dependencies = SqlSecurityHttpDependencyProvider()
    app.state.case_http_dependencies = SqlCaseHttpDependencyProvider()
    app.state.search_http_dependencies = SqlSearchHttpDependencyProvider()
    app.include_router(security_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: holder["user"]

    def _override_clock() -> _FrozenClock:
        return _FrozenClock()

    app.dependency_overrides[get_clock] = _override_clock
    return app


async def _seed_tenants(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        session.add(TenantRow(id=TENANT_A, key="ACME", name="ACME", created_at=NOW))
        session.add(TenantRow(id=TENANT_B, key="BETA", name="BETA", created_at=NOW))
        await session.commit()


async def _seed_case(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    title: str = "Audited cross-tenant matter",
) -> UUID:
    """Stage one open case in tenant B; return its id."""
    case_id = uuid4()
    async with session_factory() as session:
        await SqlCaseRepository(session).add(
            Case(
                id=case_id,
                tenant_id=TENANT_B,
                reference=CaseReference("C-BETA-2026-000001"),
                case_type_version_id=uuid4(),
                status=CaseStatus.OPEN,
                priority="normal",
                title=title,
                description="",
                data={},
                current_workflow_state=None,
                version_token=1,
                created_at=NOW,
                updated_at=NOW,
                created_by=ADMIN_B_ID,
            )
        )
        await session.commit()
    return case_id


async def _mint_grant(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scope: BreakGlassScope,
    granted_at: datetime = NOW,
    duration: timedelta = timedelta(minutes=15),
) -> BreakGlassGrant:
    """Mint a grant in tenant B for the auditor (data-owner issuance)."""
    grant = BreakGlassGrant.grant(
        id=uuid4(),
        tenant_id=TENANT_B,
        user_id=AUDITOR_ID,
        scope=scope,
        reason="Live incident — auditor investigation",
        granted_by=ADMIN_B_ID,
        granted_at=granted_at,
        duration=duration,
    )
    async with session_factory() as session:
        await SqlBreakGlassGrantRepository(session).add(grant)
        await session.commit()
    return grant


async def _flagged_actions(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[AuditEventRow]:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(AuditEventRow).where(
                        AuditEventRow.tenant_id == TENANT_B,
                        AuditEventRow.event_type == break_glass.BREAK_GLASS_ACTION,
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


def test_read__active_grant__returns_case_and_flags_audit(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Auditor with a live grant reads the tenant-B case; action flagged."""
    _run(_seed_tenants(sql_session_factory))
    case_id = _run(_seed_case(sql_session_factory))
    holder: dict[str, UserContext] = {"user": _admin_b()}
    app = _build_app(sql_session_factory, holder)

    with TestClient(app) as client:
        # when tenant B's admin mints the grant over HTTP …
        minted = client.post(
            "/api/v1/security/break-glass",
            json={
                "target_user_id": str(AUDITOR_ID),
                "scope": "cross_tenant_case_read",
                "reason": "Live incident — auditor investigation",
                "duration_minutes": 15,
            },
        )
        assert minted.status_code == 201, minted.text

        # … then the auditor reads the case cross-tenant.
        holder["user"] = _auditor()
        response = client.get(
            f"/api/v1/security/break-glass/cases/{case_id}",
            params={"target_tenant_id": str(TENANT_B)},
        )

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(case_id)
    assert response.headers["X-Break-Glass-Grant"] == minted.json()["grant_id"]

    flagged = _run(_flagged_actions(sql_session_factory))
    assert len(flagged) == 1
    assert flagged[0].payload["scope"] == "cross_tenant_case_read"
    assert flagged[0].payload["reason"] == "Live incident — auditor investigation"
    assert flagged[0].payload["resource_id"] == str(case_id)


def test_read__no_grant__denies_without_leaking_existence(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without a grant the read is 403 even for a nonexistent id (no oracle)."""
    _run(_seed_tenants(sql_session_factory))
    holder: dict[str, UserContext] = {"user": _auditor()}
    app = _build_app(sql_session_factory, holder)

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/security/break-glass/cases/{uuid4()}",
            params={"target_tenant_id": str(TENANT_B)},
        )

    assert response.status_code == 403, response.text
    assert _run(_flagged_actions(sql_session_factory)) == []


def test_read__expired_grant__denies(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A grant past its window denies (the 15-minute bound is real)."""
    _run(_seed_tenants(sql_session_factory))
    case_id = _run(_seed_case(sql_session_factory))
    _run(
        _mint_grant(
            sql_session_factory,
            scope=BreakGlassScope.CROSS_TENANT_CASE_READ,
            granted_at=NOW - timedelta(minutes=30),
            duration=timedelta(minutes=15),
        )
    )
    holder: dict[str, UserContext] = {"user": _auditor()}
    app = _build_app(sql_session_factory, holder)

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/security/break-glass/cases/{case_id}",
            params={"target_tenant_id": str(TENANT_B)},
        )

    assert response.status_code == 403, response.text
    assert "expired" in response.text


def test_read__revoked_grant__denies(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A revoked grant denies even inside its time window."""
    _run(_seed_tenants(sql_session_factory))
    case_id = _run(_seed_case(sql_session_factory))
    grant = _run(_mint_grant(sql_session_factory, scope=BreakGlassScope.CROSS_TENANT_CASE_READ))

    async def _revoke() -> None:
        async with sql_session_factory() as session:
            repo = SqlBreakGlassGrantRepository(session)
            stored = await repo.get(TENANT_B, grant.id)
            assert stored is not None
            await repo.save(stored.revoke(revoked_by=ADMIN_B_ID, revoked_at=NOW))
            await session.commit()

    _run(_revoke())
    holder: dict[str, UserContext] = {"user": _auditor()}
    app = _build_app(sql_session_factory, holder)

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/security/break-glass/cases/{case_id}",
            params={"target_tenant_id": str(TENANT_B)},
        )

    assert response.status_code == 403, response.text
    assert "revoked" in response.text


def test_read__same_tenant_target__rejected(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The override is for crossing tenants; own-tenant reads use GET /cases."""
    _run(_seed_tenants(sql_session_factory))
    holder: dict[str, UserContext] = {"user": _auditor()}
    app = _build_app(sql_session_factory, holder)

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/security/break-glass/cases/{uuid4()}",
            params={"target_tenant_id": str(TENANT_A)},
        )

    assert response.status_code == 400, response.text


def test_search__active_grant__returns_hits_and_flags_audit(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Auditor with a live search grant finds the tenant-B case; flagged."""
    _run(_seed_tenants(sql_session_factory))
    _run(_seed_case(sql_session_factory, title="Cross-tenant smuggling probe"))
    _run(_mint_grant(sql_session_factory, scope=BreakGlassScope.CROSS_TENANT_SEARCH))
    holder: dict[str, UserContext] = {"user": _auditor()}
    app = _build_app(sql_session_factory, holder)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/security/break-glass/search",
            json={
                "target_tenant_id": str(TENANT_B),
                "query": "smuggling",
                "filters": [],
                "limit": 50,
                "offset": 0,
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["title"] == "Cross-tenant smuggling probe"
    assert "X-Break-Glass-Grant" in response.headers

    flagged = _run(_flagged_actions(sql_session_factory))
    assert len(flagged) == 1
    assert flagged[0].payload["action"] == "case.search"
    assert flagged[0].payload["result_count"] == 1


def test_search__no_grant__denies(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Search without a grant is 403 (no result oracle)."""
    _run(_seed_tenants(sql_session_factory))
    _run(_seed_case(sql_session_factory))
    holder: dict[str, UserContext] = {"user": _auditor()}
    app = _build_app(sql_session_factory, holder)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/security/break-glass/search",
            json={
                "target_tenant_id": str(TENANT_B),
                "query": "matter",
                "filters": [],
                "limit": 50,
                "offset": 0,
            },
        )

    assert response.status_code == 403, response.text


def test_grant_repository__roundtrip_is_tenant_scoped(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Grants persist, resolve per tenant, and never leak across tenants."""

    async def _roundtrip() -> None:
        async with sql_session_factory() as session:
            repo = SqlBreakGlassGrantRepository(session)
            grant = BreakGlassGrant.grant(
                id=uuid4(),
                tenant_id=TENANT_B,
                user_id=AUDITOR_ID,
                scope=BreakGlassScope.CROSS_TENANT_SEARCH,
                reason="synthetic probe",
                granted_by=ADMIN_B_ID,
                granted_at=NOW,
                duration=timedelta(minutes=15),
            )
            await repo.add(grant)
            assert await repo.get(TENANT_B, grant.id) is not None
            # Cross-tenant id lookup is indistinguishable from missing.
            assert await repo.get(TENANT_A, grant.id) is None
            live = await repo.active_grant_for(
                TENANT_B, AUDITOR_ID, BreakGlassScope.CROSS_TENANT_SEARCH, now=NOW
            )
            assert live is not None
            assert live.id == grant.id
            # Wrong scope is not active.
            assert (
                await repo.active_grant_for(
                    TENANT_B,
                    AUDITOR_ID,
                    BreakGlassScope.CROSS_TENANT_CASE_READ,
                    now=NOW,
                )
                is None
            )
            await session.commit()

    _run(_roundtrip())


def test_migrated_schema__carries_break_glass_grants(tmp_path: Path) -> None:
    """Migration 0023 lands the grants table the SQL adapter speaks."""
    backend = Path(__file__).resolve().parents[2]
    db_file = tmp_path / "bg-parity.db"
    env = os.environ.copy()
    env["APP_ENV"] = "test"
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(backend),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"alembic failed:\n{proc.stdout}\n{proc.stderr}"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    try:
        columns = {c["name"] for c in sa_inspect(engine).get_columns("break_glass_grants")}
        assert {"reason", "expires_at", "revoked_at", "scope"} <= columns
    finally:
        engine.dispose()
