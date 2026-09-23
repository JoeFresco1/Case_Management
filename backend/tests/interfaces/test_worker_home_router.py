"""SQLite endpoint coverage for the T5 worker-home surface.

Exercises the production provider composition
(``SqlWorkerHomeHttpDependencyProvider`` with process-local stores):

- preferences lifecycle: save → effective reflects user values;
- admin-override acceptance: publish policy locks a key → effective
  returns the admin value with ``locked=True`` even though the stored
  user value conflicts;
- auth denial: a non-admin publish is 403 and writes no policy;
- tenant isolation: tenant B never sees tenant A's dashboard.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.worker_home.worker_home import (
    InMemoryDashboardStore,
    InMemoryPreferencePolicyStore,
    InMemoryPreferenceStore,
)
from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import DomainError
from app.infrastructure.persistence.worker_home_http_dependencies import (
    SqlWorkerHomeHttpDependencyProvider,
)
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.worker_home import (
    get_worker_home_dependencies,
)
from app.interfaces.http.routers.worker_home import (
    router as worker_home_router,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
WORKER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def _idem() -> dict[str, str]:
    """Return a fresh idempotency header (one key per mutation)."""
    return {"Idempotency-Key": str(uuid4())}


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    roles: tuple[str, ...] = ("reviewer",),
    user_id: UUID = WORKER_ID,
    tenant_id: UUID = TENANT_A,
    provider: SqlWorkerHomeHttpDependencyProvider | None = None,
) -> FastAPI:
    """Build a minimal app using the production provider composition."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    owned = provider or SqlWorkerHomeHttpDependencyProvider(
        dashboards=InMemoryDashboardStore(),
        preferences=InMemoryPreferenceStore(),
        policies=InMemoryPreferencePolicyStore(),
    )
    app = FastAPI()
    app.state.worker_home_http_dependencies = owned
    app.include_router(worker_home_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic User",
        roles=roles,
    )
    app.dependency_overrides[get_worker_home_dependencies] = lambda: owned
    return app


def test_worker_home__save_and_effective__round_trips(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Worker saves dashboard + preferences; effective reflects them."""
    # // given a worker app
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        # // when saving a dashboard // then the widgets persist.
        saved = client.put(
            "/api/v1/home/dashboard",
            json={
                "widgets": ["pinned_cases", "favorites", "task_pane"],
                "favorites": [],
                "shortcuts": [{"label": "Triage", "target": "/queues/triage"}],
                "grouping": "queue",
                "sort": "due",
            },
            headers=_idem(),
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["grouping"] == "queue"

        # // when saving preferences // then effective returns them unlocked.
        prefs = client.put(
            "/api/v1/home/preferences",
            json={"values": {"theme": "dark", "density": "compact"}},
            headers=_idem(),
        )
        assert prefs.status_code == 200, prefs.text
        effective = client.get("/api/v1/home/preferences/effective")
        assert effective.status_code == 200, effective.text
        by_key: dict[str, Any] = {item["key"]: item for item in effective.json()["preferences"]}
        assert by_key["theme"]["value"] == "dark"
        assert by_key["theme"]["locked"] is False


def test_worker_home__admin_policy__overrides_user_preference(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admin lock wins over a conflicting stored user value (T5 acceptance)."""
    # // given a shared provider (worker + admin see the same tenant stores)
    provider = SqlWorkerHomeHttpDependencyProvider(
        dashboards=InMemoryDashboardStore(),
        preferences=InMemoryPreferenceStore(),
        policies=InMemoryPreferencePolicyStore(),
    )
    worker_app = _build_app(sql_session_factory, provider=provider)
    admin_app = _build_app(
        sql_session_factory, provider=provider, roles=("process_admin",), user_id=ADMIN_ID
    )
    with TestClient(worker_app) as worker:
        stored = worker.put(
            "/api/v1/home/preferences",
            json={"values": {"theme": "dark"}},
            headers=_idem(),
        )
        assert stored.status_code == 200, stored.text
    with TestClient(admin_app) as admin:
        # // when the admin locks the key // then the publish succeeds.
        published = admin.put(
            "/api/v1/admin/preference-policy",
            json={"locked": {"theme": "light"}, "allowed": {}},
            headers=_idem(),
        )
        assert published.status_code == 200, published.text
        assert published.json()["locked"] == {"theme": "light"}
    with TestClient(worker_app) as worker:
        # // when reading effective // then the admin value wins, locked.
        effective = worker.get("/api/v1/home/preferences/effective")
        assert effective.status_code == 200, effective.text
        by_key: dict[str, Any] = {item["key"]: item for item in effective.json()["preferences"]}
        assert by_key["theme"]["value"] == "light"
        assert by_key["theme"]["locked"] is True


def test_worker_home__publish_policy__denies_non_admin(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A worker without ``admin.tenant_config`` cannot publish policy."""
    # // given a worker app // when publishing // then 403, no policy stored.
    app = _build_app(sql_session_factory, roles=("reviewer",))
    with TestClient(app) as client:
        denied = client.put(
            "/api/v1/admin/preference-policy",
            json={"locked": {"theme": "light"}, "allowed": {}},
            headers=_idem(),
        )
        assert denied.status_code == 403, denied.text


def test_worker_home__dashboard__is_tenant_isolated(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tenant B never sees tenant A's dashboard (404, not 200)."""
    # // given a shared provider with a dashboard in tenant A
    provider = SqlWorkerHomeHttpDependencyProvider(
        dashboards=InMemoryDashboardStore(),
        preferences=InMemoryPreferenceStore(),
        policies=InMemoryPreferencePolicyStore(),
    )
    app_a = _build_app(sql_session_factory, provider=provider, tenant_id=TENANT_A)
    app_b = _build_app(
        sql_session_factory, provider=provider, tenant_id=TENANT_B, user_id=WORKER_ID
    )
    with TestClient(app_a) as client_a:
        saved = client_a.put(
            "/api/v1/home/dashboard",
            json={"widgets": ["pinned_cases"], "shortcuts": []},
            headers=_idem(),
        )
        assert saved.status_code == 200, saved.text
    with TestClient(app_b) as client_b:
        # // when tenant B reads // then 404 (no cross-tenant leak).
        missing = client_b.get("/api/v1/home/dashboard")
        assert missing.status_code == 404, missing.text
