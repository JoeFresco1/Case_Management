"""HTTP acceptance tests for atomic, tenant-scoped idempotency.

These tests deliberately use a small SQLite mutation table instead of a
business use case. The behavior under test belongs to the HTTP transaction
boundary: the idempotency provider and the mutation must share the same
``AsyncSession`` and commit exactly once.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from sqlalchemy import Column, Integer, String, Table, Uuid, insert, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.config as app_config
from app.db import Base
from app.deps import get_db, register_local_user
from app.domain.context import UserContext
from app.infrastructure.persistence.idempotency_http_dependencies import (
    SqlIdempotencyHttpDependencyProvider,
)
from app.infrastructure.telemetry.correlation import clear
from app.interfaces.http.idempotency import IdempotencyMiddleware

# Importing the composition-root middleware is intentionally avoided here:
# these tests target idempotency's transaction contract and must remain
# runnable while the parent refactors application startup wiring.

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
USER_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
USER_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

MUTATIONS = Table(
    "test_idempotency_mutations",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant_id", Uuid, nullable=False),
    Column("route", String, nullable=False),
    Column("body", String, nullable=False),
)


@pytest.fixture
async def idempotency_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Create an isolated SQLite schema for each idempotency scenario."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'idempotency.db'}",
        pool_pre_ping=True,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        # Ensure all connections are returned to the pool before disposal.
        # Wait for pool to drain by disposing with close=True.
        await engine.dispose(close=True)


@pytest.fixture
async def idempotency_app(
    idempotency_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[FastAPI, async_sessionmaker[AsyncSession]]]:
    """Build a FastAPI app with the real SQL provider and shared session factory."""
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./idempotency-test.db")
    app_config._settings = None
    register_local_user(UserContext(USER_A, TENANT_A, "Synthetic A", ("intake",)))
    register_local_user(UserContext(USER_B, TENANT_B, "Synthetic B", ("intake",)))

    session_factory = async_sessionmaker(
        bind=idempotency_engine,
        expire_on_commit=False,
        autoflush=False,
    )
    provider = SqlIdempotencyHttpDependencyProvider(sessionmaker=session_factory)
    app = FastAPI()
    app.state.idempotency_http_dependencies = provider

    async def shared_db(request: Request) -> AsyncIterator[AsyncSession]:
        """Yield the middleware-owned session for the mutation route."""
        session = getattr(request.state, "idempotency_session", None)
        if session is None:
            raise RuntimeError("idempotency session was not installed")
        yield session

    app.dependency_overrides[get_db] = shared_db

    async def mutate(
        request: Request,
        db: AsyncSession = Depends(get_db),  # noqa: B008
    ) -> dict[str, object]:
        """Record one toy mutation and commit only through the outer boundary."""
        user = request.state.user
        body = (await request.body()).decode("utf-8")
        await db.execute(
            insert(MUTATIONS).values(
                tenant_id=user.tenant_id,
                route=request.url.path,
                body=body,
            )
        )
        await db.flush()
        # The production get_db adapter uses rollback-only join semantics, so
        # route commits flush without committing the middleware's transaction.
        await db.commit()
        return {"route": request.url.path, "body": body}

    app.add_api_route("/api/v1/mutate", mutate, methods=["POST", "PATCH", "PUT", "DELETE"])
    app.add_api_route("/api/v1/other", mutate, methods=["POST", "PATCH", "PUT", "DELETE"])

    async def query_post(request: Request) -> dict[str, str]:
        """Represent a route that performs its own query or bearer-token authorization."""
        return {"path": request.url.path}

    app.add_api_route("/api/v1/search", query_post, methods=["POST"])
    app.add_api_route("/api/v1/search/semantic", query_post, methods=["POST"])
    app.add_api_route("/api/v1/search/saved/{saved_search_id}/run", query_post, methods=["POST"])
    app.add_api_route(
        "/api/v1/admin/workflows/{workflow_id}/versions/{version_number}/simulate",
        query_post,
        methods=["POST"],
    )
    app.add_api_route("/api/v1/bundles/links/redeem", query_post, methods=["POST"])
    app.add_middleware(IdempotencyMiddleware, scope_paths=("/api/v1/",))
    try:
        yield app, session_factory
    finally:
        clear()
        app_config._settings = None


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an in-process HTTP client without opening a network socket.

    Consumed with ``async with`` (not ``async for``) so the client's
    ``aclose`` runs deterministically inside the test's event loop.
    Relying on async-generator finalization instead lets GC timing
    close the underlying connection pool on a *later* loop, which
    surfaces as an intermittent teardown ERROR under
    ``filterwarnings = error``.
    """
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _headers(user_id: UUID, tenant_id: UUID, key: str) -> dict[str, str]:
    """Build local synthetic identity and idempotency headers."""
    return {
        "X-Dev-User": str(user_id),
        "X-Dev-Tenant": str(tenant_id),
        "Idempotency-Key": key,
    }


async def _rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[dict[str, object]]:
    """Read toy mutation rows for assertions without relying on app state."""
    async with session_factory() as session:
        result = await session.execute(select(MUTATIONS).order_by(MUTATIONS.c.id))
        return [dict(row) for row in result.mappings().all()]


@pytest.mark.asyncio
async def test_same_key_same_request__executes_once_and_replays(
    idempotency_app: tuple[FastAPI, async_sessionmaker[AsyncSession]],
) -> None:
    """A successful retry returns the original response and one mutation row."""
    app, factory = idempotency_app
    async with _client(app) as client:
        first = await client.post(
            "/api/v1/mutate",
            content="alpha",
            headers=_headers(USER_A, TENANT_A, "same-key"),
        )
        second = await client.post(
            "/api/v1/mutate",
            content="alpha",
            headers=_headers(USER_A, TENANT_A, "same-key"),
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers["idempotency-replay"] == "true"
    assert second.json() == first.json()
    assert len(await _rows(factory)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
async def test_mutating_method_without_key__is_rejected(
    method: str,
    idempotency_app: tuple[FastAPI, async_sessionmaker[AsyncSession]],
) -> None:
    """Every state-changing HTTP verb requires the same replay key contract."""
    app, factory = idempotency_app
    async with _client(app) as client:
        response = await client.request(
            method,
            "/api/v1/mutate",
            content="alpha",
            headers={"X-Dev-User": str(USER_A), "X-Dev-Tenant": str(TENANT_A)},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "validation_failed"
    assert await _rows(factory) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
async def test_mutating_method_with_key__executes_once_and_replays(
    method: str,
    idempotency_app: tuple[FastAPI, async_sessionmaker[AsyncSession]],
) -> None:
    """Each mutation verb buffers and replays its original response."""
    app, factory = idempotency_app
    async with _client(app) as client:
        first = await client.request(
            method,
            "/api/v1/mutate",
            content="alpha",
            headers=_headers(USER_A, TENANT_A, f"{method.lower()}-key"),
        )
        second = await client.request(
            method,
            "/api/v1/mutate",
            content="alpha",
            headers=_headers(USER_A, TENANT_A, f"{method.lower()}-key"),
        )
    assert first.status_code == second.status_code == 200
    assert second.headers["idempotency-replay"] == "true"
    assert second.json() == first.json()
    assert len(await _rows(factory)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/search",
        "/api/v1/search/semantic",
        "/api/v1/search/saved/11111111-1111-1111-1111-111111111111/run",
        "/api/v1/admin/workflows/11111111-1111-1111-1111-111111111111/versions/1/simulate",
        "/api/v1/bundles/links/redeem",
    ],
)
async def test_query_and_bearer_post_without_key__reaches_route(
    path: str,
    idempotency_app: tuple[FastAPI, async_sessionmaker[AsyncSession]],
) -> None:
    """Read-only POSTs and the no-auth redemption path bypass mutation handling."""
    app, _ = idempotency_app
    async with _client(app) as client:
        response = await client.post(path, json={})
    assert response.status_code == 200
    assert response.json() == {"path": path}


@pytest.mark.asyncio
async def test_saved_search_creation_without_key__remains_protected(
    idempotency_app: tuple[FastAPI, async_sessionmaker[AsyncSession]],
) -> None:
    """The query exemption does not accidentally cover saved-search creation."""
    app, _ = idempotency_app
    async with _client(app) as client:
        response = await client.post("/api/v1/search/saved", json={})
    assert response.status_code == 400
    assert response.json()["code"] == "validation_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [("/api/v1/mutate", "beta"), ("/api/v1/other", "alpha")],
)
async def test_same_key_different_request__conflicts(
    path: str,
    body: str,
    idempotency_app: tuple[FastAPI, async_sessionmaker[AsyncSession]],
) -> None:
    """Changing payload or operation path cannot reuse an idempotency key."""
    app, factory = idempotency_app
    async with _client(app) as client:
        first = await client.post(
            "/api/v1/mutate",
            content="alpha",
            headers=_headers(USER_A, TENANT_A, "conflict-key"),
        )
        second = await client.post(
            path,
            content=body,
            headers=_headers(USER_A, TENANT_A, "conflict-key"),
        )
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "conflict"
    assert len(await _rows(factory)) == 1


@pytest.mark.asyncio
async def test_same_key_different_tenants__is_independent(
    idempotency_app: tuple[FastAPI, async_sessionmaker[AsyncSession]],
) -> None:
    """The same key may be used independently by two tenants."""
    app, factory = idempotency_app
    async with _client(app) as client:
        tenant_a = await client.post(
            "/api/v1/mutate",
            content="alpha",
            headers=_headers(USER_A, TENANT_A, "shared-key"),
        )
        tenant_b = await client.post(
            "/api/v1/mutate",
            content="alpha",
            headers=_headers(USER_B, TENANT_B, "shared-key"),
        )
    assert tenant_a.status_code == tenant_b.status_code == 200
    rows = await _rows(factory)
    assert {row["tenant_id"] for row in rows} == {TENANT_A, TENANT_B}


@pytest.mark.asyncio
async def test_missing_auth__cannot_replay_cached_response(
    idempotency_app: tuple[FastAPI, async_sessionmaker[AsyncSession]],
) -> None:
    """Authentication must happen before a cached response is consulted."""
    app, factory = idempotency_app
    async with _client(app) as client:
        first = await client.post(
            "/api/v1/mutate",
            content="alpha",
            headers=_headers(USER_A, TENANT_A, "auth-key"),
        )
        replay_without_auth = await client.post(
            "/api/v1/mutate",
            content="alpha",
            headers={"Idempotency-Key": "auth-key"},
        )
    assert first.status_code == 200
    assert replay_without_auth.status_code == 401
    assert len(await _rows(factory)) == 1


@pytest.mark.asyncio
async def test_provider_failure_during_response_finalization__rolls_back_mutation(
    idempotency_app: tuple[FastAPI, async_sessionmaker[AsyncSession]],
) -> None:
    """A failure storing the replay record rolls back the toy mutation too."""
    app, factory = idempotency_app

    class FailingProvider(SqlIdempotencyHttpDependencyProvider):
        """Fail after the real provider stages the idempotency row."""

        @asynccontextmanager
        async def transaction(self, **kwargs: object) -> AsyncIterator[object]:
            """Raise after real finalization so the outer transaction rolls back."""
            async with super().transaction(**kwargs) as transaction:
                original = transaction

                class FailingTransaction:
                    """Proxy the real transaction and fail during finalization."""

                    session = original.session
                    prior = original.prior

                    async def finalize(
                        self,
                        response_status: int,
                        response_body: object,
                        correlation_id: str,
                    ) -> None:
                        """Inject a deterministic response-finalization failure."""
                        await original.finalize(response_status, response_body, correlation_id)
                        raise RuntimeError("synthetic finalization failure")

                yield FailingTransaction()

    app.state.idempotency_http_dependencies = FailingProvider(
        sessionmaker=factory,
    )
    async with _client(app) as client:
        response = await client.post(
            "/api/v1/mutate",
            content="alpha",
            headers=_headers(USER_A, TENANT_A, "failure-key"),
        )
    assert response.status_code == 500
    assert await _rows(factory) == []


@pytest.mark.asyncio
async def test_concurrent_same_key__executes_mutation_once(
    idempotency_app: tuple[FastAPI, async_sessionmaker[AsyncSession]],
) -> None:
    """Concurrent retries are serialized by the durable idempotency key."""
    app, factory = idempotency_app
    async with _client(app) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/v1/mutate",
                    content="alpha",
                    headers=_headers(USER_A, TENANT_A, "concurrent-key"),
                )
                for _ in range(2)
            )
        )
    assert {response.status_code for response in responses} == {200}
    assert len(await _rows(factory)) == 1
    assert sum(response.headers.get("idempotency-replay") == "true" for response in responses) == 1
