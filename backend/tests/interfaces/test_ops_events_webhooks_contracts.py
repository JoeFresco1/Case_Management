"""HTTP contracts for the operability surface (Run G / T7-T8).

Covers ``/api/v1/admin/outbox`` (inspection + replay), ``/events/catalog``,
``/webhooks/subscriptions`` (CRUD + pagination + concurrency), ``/graphql``
(denial/compatibility smoke), ``/admin/system`` (diagnostics + conflict),
and ``/extensions`` (registration + catalogs).

Every mutation requires ``Idempotency-Key``; versioned writes honor
``If-Match``; list endpoints honor ``limit`` (default 50, max 200);
failures use the structured ``ErrorResponse`` envelope (never SQL/stack).

Naming: ``test_<unit>_<scenario>__<expectation>`` with given/when/then.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.configuration.system_settings import InMemorySystemSettingsStore
from app.application.outbox.http_dependencies import OutboxRowView
from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import DomainError
from app.domain.extension import build_registry
from app.infrastructure.persistence.admin_outbox_http_dependencies import (
    SqlAdminOutboxHttpDependencyProvider,
)
from app.infrastructure.persistence.audit_http_dependencies import (
    SqlAuditHttpDependencyProvider,
)
from app.infrastructure.persistence.case_http_dependencies import (
    SqlCaseHttpDependencyProvider,
)
from app.infrastructure.persistence.webhooks_http_dependencies import (
    SqlWebhookHttpDependencyProvider,
)
from app.infrastructure.system.local_system_adapters import (
    LocalDiagnosticsService,
    LocalEncryptionAtRest,
    LocalStorageVolume,
)
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers import admin_outbox as admin_outbox_router_mod
from app.interfaces.http.routers import extensions as extensions_router_mod
from app.interfaces.http.routers.admin_outbox import router as outbox_router
from app.interfaces.http.routers.admin_system import router as system_router
from app.interfaces.http.routers.events import router as events_router
from app.interfaces.http.routers.extensions import router as extensions_router
from app.interfaces.http.routers.graphql import router as graphql_router
from app.interfaces.http.routers.webhooks import router as webhooks_router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ADMIN_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
WORKER_A = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _user(
    user_id: UUID = ADMIN_A,
    tenant_id: UUID = TENANT_A,
    roles: tuple[str, ...] = ("process_admin",),
) -> UserContext:
    """Build a synthetic actor (process_admin holds ADMIN_TENANT_CONFIG)."""
    return UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic User",
        roles=roles,
    )


def _override_db(session_factory: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    """Return a get_db override sharing the sqlite test schema."""

    async def _gen() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    return _gen


# ---------------------------------------------------------------------------
# Outbox helpers
# ---------------------------------------------------------------------------


class _FakeOutboxService:
    """In-memory OutboxAdminService (inspection + replay, tenant-labeled)."""

    def __init__(self, rows: list[OutboxRowView]) -> None:
        self._rows = {r.id: r for r in rows}
        self._reset_count = 0

    async def count_lag(self) -> dict[str, int]:
        """Count pending / dead-lettered / retrying across all tenants."""
        pending = [r for r in self._rows.values() if r.dispatched_at is None]
        dead = [r for r in pending if r.attempt_count >= 5]
        retrying = [r for r in pending if r.last_error and r.attempt_count < 5]
        return {
            "total": len(self._rows),
            "pending": len(pending),
            "dead_lettered": len(dead),
            "retrying": len(retrying),
        }

    async def list_pending(
        self, *, include_dead_lettered: bool = True, limit: int = 50
    ) -> Sequence[OutboxRowView]:
        """Return pending rows oldest-first, honoring the page limit."""
        rows = sorted(
            (r for r in self._rows.values() if r.dispatched_at is None),
            key=lambda r: r.created_at,
        )
        if not include_dead_lettered:
            rows = [r for r in rows if r.attempt_count < 5]
        return rows[:limit]

    async def replay(self, row_id: UUID) -> bool:
        """Reset attempt state; second replay before dispatch is a no-op."""
        row = self._rows.get(row_id)
        if row is None or row.dispatched_at is not None:
            return False
        if row.attempt_count == 0:
            return False
        self._rows[row_id] = OutboxRowView(
            id=row.id,
            tenant_id=row.tenant_id,
            event_type=row.event_type,
            event_id=row.event_id,
            created_at=row.created_at,
            dispatched_at=row.dispatched_at,
            attempt_count=0,
            last_error=row.last_error,
        )
        self._reset_count += 1
        return True


def _outbox_row(**overrides: Any) -> OutboxRowView:
    """Build one pending outbox row (metadata only — no payload)."""
    base: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": TENANT_A,
        "event_type": "CaseCreated",
        "event_id": uuid4(),
        "created_at": NOW,
        "dispatched_at": None,
        "attempt_count": 5,
        "last_error": "transport timeout",
    }
    base.update(overrides)
    return OutboxRowView(**base)


def _build_outbox_app(
    session_factory: async_sessionmaker[AsyncSession],
    service: Any | None,
    *,
    roles: tuple[str, ...] = ("process_admin",),
) -> FastAPI:
    """Build a minimal app with the outbox router + SQL role provider."""
    app = FastAPI()
    app.state.admin_outbox_http_dependencies = SqlAdminOutboxHttpDependencyProvider()
    if service is not None:
        app.state.outbox_dispatcher = service
    app.include_router(outbox_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_db(session_factory)
    app.dependency_overrides[get_current_user] = lambda: _user(roles=roles)
    return app


# ---------------------------------------------------------------------------
# Outbox inspection + replay
# ---------------------------------------------------------------------------


def test_outbox_list__pagination_limit__honored(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Operator pages pending rows; limit truncates, lag counters agree."""
    # // given three pending rows (two dead-lettered, one retrying)
    rows = [_outbox_row(), _outbox_row(), _outbox_row(attempt_count=1)]
    app = _build_outbox_app(sql_session_factory, _FakeOutboxService(rows))
    # // when requesting a page of two
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/outbox?limit=2")
    # // then the page holds two rows and the lag summary counts all three
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 2
    assert body["limit"] == 2
    assert body["lag"]["total"] == 3
    assert body["lag"]["dead_lettered"] == 2
    assert body["lag"]["retrying"] == 1


def test_outbox_list__rows_expose_tenant_metadata_without_payload(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rows carry tenant_id for labeling; payloads never leave the process."""
    # // given rows from two tenants
    rows = [_outbox_row(tenant_id=TENANT_A), _outbox_row(tenant_id=TENANT_B)]
    app = _build_outbox_app(sql_session_factory, _FakeOutboxService(rows))
    # // when the operator lists the outbox
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/outbox")
    # // then both tenant labels appear and no payload key is exposed
    assert response.status_code == 200, response.text
    tenants = {item["tenant_id"] for item in response.json()["items"]}
    assert tenants == {str(TENANT_A), str(TENANT_B)}
    assert all("payload" not in item for item in response.json()["items"])


def test_outbox_replay__reset_then_noop__idempotent(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """First replay resets; a second replay before dispatch is a no-op."""
    # // given one dead-lettered row
    row = _outbox_row()
    app = _build_outbox_app(sql_session_factory, _FakeOutboxService([row]))
    # // when replaying twice
    with TestClient(app) as client:
        first = client.post(f"/api/v1/admin/outbox/{row.id}/replay")
        second = client.post(f"/api/v1/admin/outbox/{row.id}/replay")
    # // then the first resets and the second reports no-op
    assert first.status_code == 200, first.text
    assert first.json()["reset"] is True
    assert second.status_code == 200, second.text
    assert second.json()["reset"] is False


def test_outbox__non_admin__denied_with_forbidden(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A worker without ADMIN_TENANT_CONFIG cannot inspect or replay."""
    # // given a worker actor and one pending row
    row = _outbox_row()
    app = _build_outbox_app(sql_session_factory, _FakeOutboxService([row]), roles=("intake",))
    # // when the worker lists and replays
    with TestClient(app) as client:
        listed = client.get("/api/v1/admin/outbox")
        replayed = client.post(f"/api/v1/admin/outbox/{row.id}/replay")
    # // then both are 403 forbidden with no row data
    assert listed.status_code == 403, listed.text
    assert listed.json()["code"] == "forbidden"
    assert replayed.status_code == 403, replayed.text


def test_outbox__dispatcher_missing__dependency_unavailable(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Without a wired dispatcher the console reports 503, not 404."""
    # // given an app with no dispatcher on state
    app = _build_outbox_app(sql_session_factory, None)
    # // when the operator lists the outbox
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/outbox")
    # // then the dependency failure is explicit
    assert response.status_code == 503, response.text


# ---------------------------------------------------------------------------
# Event catalog
# ---------------------------------------------------------------------------


def _build_events_app(*, authenticated: bool = True) -> FastAPI:
    """Build a minimal app with the event catalog router."""
    app = FastAPI()
    app.include_router(events_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    if authenticated:

        def _authenticated() -> UserContext:
            return _user()

        app.dependency_overrides[get_current_user] = _authenticated
    else:

        def _deny() -> UserContext:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing X-Dev-User header",
            )

        app.dependency_overrides[get_current_user] = _deny
    return app


def test_event_catalog__authenticated__returns_versioned_entries() -> None:
    """Catalog entries carry version + payload_keys (compatibility anchor)."""
    # // given an authenticated actor
    app = _build_events_app()
    # // when fetching the catalog
    with TestClient(app) as client:
        response = client.get("/api/v1/events/catalog")
    # // then every entry is versioned with a payload whitelist
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "EVENT_CATALOG"
    assert len(body["items"]) > 0
    for item in body["items"]:
        assert isinstance(item["event_type"], str)
        assert item["event_type"]
        assert isinstance(item["version"], int)
        assert item["version"] >= 1
        assert isinstance(item["payload_keys"], list)


def test_event_catalog__limit__truncates_window() -> None:
    """Pagination limit truncates the catalog window deterministically."""
    # // given an authenticated actor
    app = _build_events_app()
    # // when requesting a single entry
    with TestClient(app) as client:
        response = client.get("/api/v1/events/catalog?limit=1")
    # // then exactly one entry returns with the honored limit echoed
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    assert body["limit"] == 1
    assert body["next_cursor"] is None


def test_event_catalog__unauthenticated__denied() -> None:
    """Anonymous callers cannot enumerate the event surface."""
    # // given an app whose identity dependency denies the caller
    app = _build_events_app(authenticated=False)
    # // when the anonymous caller fetches the catalog
    with TestClient(app) as client:
        response = client.get("/api/v1/events/catalog")
    # // then the standard auth handler answers 401
    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# Webhook subscriptions
# ---------------------------------------------------------------------------


def _build_webhooks_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    roles: tuple[str, ...] = ("process_admin",),
    tenant_id: UUID = TENANT_A,
    user_id: UUID = ADMIN_A,
) -> FastAPI:
    """Build a minimal app with the webhooks router + SQL providers."""
    app = FastAPI()
    app.state.webhook_http_dependencies = SqlWebhookHttpDependencyProvider()
    app.include_router(webhooks_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_db(session_factory)
    app.dependency_overrides[get_current_user] = lambda: _user(
        user_id=user_id, tenant_id=tenant_id, roles=roles
    )
    return app


def _sub_body(**overrides: Any) -> dict[str, Any]:
    """Valid subscription body (CaseCreated is in the built-in catalog)."""
    base: dict[str, Any] = {
        "url": "https://hooks.example.test/deliveries",
        "event_filters": ["CaseCreated"],
        "secret_name": "webhook_secret_probe",
        "signing_algorithm": "hmac_sha256",
        "require_bearer": False,
        "description": "Probe subscription",
    }
    base.update(overrides)
    return base


def test_webhook_subscriptions__crud_pagination_concurrency__contract(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Create → list → update (If-Match) → stale 409 → tenant isolation."""
    # // given an admin app in tenant A
    app = _build_webhooks_app(sql_session_factory)
    with TestClient(app) as client:
        # // when creating a subscription
        created = client.post(
            "/api/v1/webhooks/subscriptions",
            json=_sub_body(),
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert created.status_code == 201, created.text
        sub = created.json()
        assert sub["status"] == "active"
        assert sub["version_token"] == 0
        assert sub["tenant_id"] == str(TENANT_A)
        sub_id = sub["id"]

        # // when listing with pagination
        listed = client.get("/api/v1/webhooks/subscriptions?limit=50")
        assert listed.status_code == 200, listed.text
        assert listed.json()["limit"] == 50
        assert any(item["id"] == sub_id for item in listed.json()["items"])

        # // when updating with a matching If-Match header
        updated = client.patch(
            f"/api/v1/webhooks/subscriptions/{sub_id}",
            json={"version_token": 0, "description": "Updated label"},
            headers={"If-Match": "0"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["version_token"] == 1

        # // when updating with a stale token the platform refuses
        stale = client.patch(
            f"/api/v1/webhooks/subscriptions/{sub_id}",
            json={"version_token": 0, "description": "Stale write"},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "conflict"

        # // when the header disagrees with the body the platform refuses
        mismatch = client.patch(
            f"/api/v1/webhooks/subscriptions/{sub_id}",
            json={"version_token": 1, "description": "Mismatch"},
            headers={"If-Match": "0"},
        )
        assert mismatch.status_code == 409, mismatch.text


def test_webhook_subscription_create__missing_idempotency_key__rejected(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutations without Idempotency-Key fail closed with 400."""
    # // given an admin app
    app = _build_webhooks_app(sql_session_factory)
    # // when creating without the header
    with TestClient(app) as client:
        response = client.post("/api/v1/webhooks/subscriptions", json=_sub_body())
    # // then the contract error names the missing header
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "missing_idempotency_key"


def test_webhook_subscription_create__unknown_event_filter__unprocessable(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A typo in event_filters is 422, never a silent no-delivery row."""
    # // given an admin app
    app = _build_webhooks_app(sql_session_factory)
    # // when registering with an unknown filter
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/webhooks/subscriptions",
            json=_sub_body(event_filters=["NoSuchEvent"]),
            headers={"Idempotency-Key": str(uuid4())},
        )
    # // then validation fails closed (compatibility guard)
    assert response.status_code == 422, response.text


def test_webhook_subscriptions__non_admin__denied(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Workers without the config right cannot manage subscriptions."""
    # // given a worker app
    app = _build_webhooks_app(sql_session_factory, roles=("intake",))
    # // when the worker creates and lists
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/webhooks/subscriptions",
            json=_sub_body(),
            headers={"Idempotency-Key": str(uuid4())},
        )
        listed = client.get("/api/v1/webhooks/subscriptions")
    # // then both are 403 forbidden
    assert created.status_code == 403, created.text
    assert listed.status_code == 403, listed.text


def test_webhook_subscription__cross_tenant__not_found(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant-B actor cannot observe a tenant-A subscription id."""
    # // given a subscription created in tenant A
    app_a = _build_webhooks_app(sql_session_factory)
    with TestClient(app_a) as client:
        created = client.post(
            "/api/v1/webhooks/subscriptions",
            json=_sub_body(),
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert created.status_code == 201, created.text
        sub_id = created.json()["id"]
    # // when a tenant-B admin patches the same id
    app_b = _build_webhooks_app(sql_session_factory, tenant_id=TENANT_B)
    with TestClient(app_b) as client:
        response = client.patch(
            f"/api/v1/webhooks/subscriptions/{sub_id}",
            json={"version_token": 0, "description": "Cross-tenant write"},
        )
    # // then existence is not confirmed (404, no leak)
    assert response.status_code == 404, response.text


def test_webhook_subscription_disable_retire__require_idempotency_key(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Disable/retire/replay without Idempotency-Key are 400."""
    # // given a subscription in tenant A
    app = _build_webhooks_app(sql_session_factory)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/webhooks/subscriptions",
            json=_sub_body(),
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert created.status_code == 201, created.text
        sub_id = created.json()["id"]
        # // when calling lifecycle endpoints without the header
        disabled = client.post(f"/api/v1/webhooks/subscriptions/{sub_id}/disable")
        retired = client.post(f"/api/v1/webhooks/subscriptions/{sub_id}/retire")
        replayed = client.post(f"/api/v1/webhooks/subscriptions/{sub_id}/replay")
    # // then each fails closed on the missing key
    assert disabled.status_code == 400, disabled.text
    assert retired.status_code == 400, retired.text
    assert replayed.status_code == 400, replayed.text


# ---------------------------------------------------------------------------
# Governed queries (GraphQL smoke — full matrix lives in test_graphql_router)
# ---------------------------------------------------------------------------


def _build_graphql_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    authenticated: bool = True,
) -> FastAPI:
    """Build a minimal app with the governed GraphQL router."""
    app = FastAPI()
    app.state.case_http_dependencies = SqlCaseHttpDependencyProvider()
    app.include_router(graphql_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_db(session_factory)
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: _user(roles=("intake",))
    else:

        def _deny() -> UserContext:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing X-Dev-User header",
            )

        app.dependency_overrides[get_current_user] = _deny
    return app


def test_graphql__variables__rejected_with_400(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The governed surface inlines arguments; variables fail closed."""
    # // given an authenticated app
    app = _build_graphql_app(sql_session_factory)
    # // when supplying a variables payload
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/graphql",
            json={"query": "query { cases(limit: 10, offset: 0) { id } }", "variables": {"a": 1}},
        )
    # // then the router rejects instead of silently ignoring
    assert response.status_code == 400, response.text


def test_graphql__unauthenticated__returns_401(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Anonymous callers never reach the executor."""
    # // given an app whose identity dependency denies the caller
    app = _build_graphql_app(sql_session_factory, authenticated=False)
    # // when posting a well-formed query
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/graphql",
            json={"query": "query { cases(limit: 10, offset: 0) { id } }"},
        )
    # // then auth fails before execution
    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# Diagnostics + system settings
# ---------------------------------------------------------------------------


def _build_system_app(
    store: InMemorySystemSettingsStore,
    *,
    roles: tuple[str, ...] = ("process_admin",),
    tmp_path: Path | None = None,
) -> FastAPI:
    """Build a minimal app with the system router + local adapters."""
    app = FastAPI()
    app.state.system_settings_store = store
    root = tmp_path if tmp_path is not None else Path("./documents")
    app.state.system_diagnostics = LocalDiagnosticsService()
    app.state.storage_volume = LocalStorageVolume(root=root)
    app.state.encryption_at_rest = LocalEncryptionAtRest()
    app.include_router(system_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_current_user] = lambda: _user(roles=roles)
    return app


def test_diagnostics__reports_cache_services_and_synthetic_marker(tmp_path: Path) -> None:
    """Diagnostics carries cache stats + desired/actual services + marker."""
    # // given the wired app with a primed diagnostics cache
    app = _build_system_app(InMemorySystemSettingsStore(), tmp_path=tmp_path)
    app.state.system_diagnostics.put("probe", {"ok": True})
    # // when reading diagnostics
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/system/diagnostics")
    # // then the operator sees cache + services + synthetic marker
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cache"]["entries"] == 1
    assert "outbox" in body["services_desired"]
    assert "outbox" in body["services_actual"]
    assert isinstance(body["synthetic_environment"], bool)


def test_system_setting_update__stale_version__conflict(tmp_path: Path) -> None:
    """Concurrent config edits refuse with structured 409 conflict."""
    # // given version 1 of a setting
    app = _build_system_app(InMemorySystemSettingsStore(), tmp_path=tmp_path)
    with TestClient(app) as client:
        # // when writing a valid value with a stale expected_version
        response = client.put(
            "/api/v1/admin/system/settings/diagnostics.cache",
            json={
                "value": {"max_entries": 500, "ttl_seconds": 300},
                "expected_version": 99,
                "change_summary": "stale",
            },
        )
    # // then the platform refuses instead of silently overwriting
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "conflict"


def test_system_diagnostics__non_admin__denied(tmp_path: Path) -> None:
    """Workers cannot read diagnostics or storage posture."""
    # // given a worker app
    app = _build_system_app(InMemorySystemSettingsStore(), roles=("intake",), tmp_path=tmp_path)
    # // when reading diagnostics and storage
    with TestClient(app) as client:
        diagnostics = client.get("/api/v1/admin/system/diagnostics")
        storage = client.get("/api/v1/admin/system/storage")
    # // then both are 403 forbidden
    assert diagnostics.status_code == 403, diagnostics.text
    assert storage.status_code == 403, storage.text


# ---------------------------------------------------------------------------
# Extension registration
# ---------------------------------------------------------------------------


def _build_extensions_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    roles: tuple[str, ...] = ("process_admin",),
    fresh_registry: bool = True,
    monkeypatch: Any = None,
) -> FastAPI:
    """Build a minimal app with the extensions router + SQL audit store."""
    app = FastAPI()
    app.state.audit_http_dependencies = SqlAuditHttpDependencyProvider()
    app.include_router(extensions_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    # Map ExtensionConflictError (plain Exception) to 409 conflict so the
    # HTTP boundary speaks one error dialect for duplicate installs.
    from app.domain.extension import ExtensionConflictError  # noqa: PLC0415

    async def _conflict_handler(request: Any, exc: ExtensionConflictError) -> Any:
        from app.interfaces.http.errors import error_response  # noqa: PLC0415

        cid = getattr(getattr(request, "state", None), "correlation", None)
        correlation_id = str(getattr(cid, "correlation_id", "") or "")
        return error_response(
            code="conflict",
            message=str(exc),
            status_code=409,
            correlation_id=correlation_id,
        )

    app.add_exception_handler(ExtensionConflictError, _conflict_handler)  # type: ignore[arg-type]
    app.dependency_overrides[get_db] = _override_db(session_factory)
    app.dependency_overrides[get_current_user] = lambda: _user(roles=roles)
    if fresh_registry and monkeypatch is not None:
        fresh = build_registry()
        monkeypatch.setattr(extensions_router_mod, "get_registry", lambda: fresh)
        # The use case resolves the registry through the same symbol.
        import app.application.extensions.register_extension as reg_mod  # noqa: PLC0415

        monkeypatch.setattr(reg_mod, "get_registry", lambda: fresh)
    return app


def _extension_body(module_id: str = "ext.probe-contracts", **overrides: Any) -> dict[str, Any]:
    """Valid extension body (namespaced action + event type)."""
    base: dict[str, Any] = {
        "module_id": module_id,
        "version": 1,
        "description": "Probe extension for contract tests",
        "author": "Synthetic Suite",
        "policy_actions": [
            {
                "action": f"{module_id}.review",
                "description": "Review probe items",
                "is_sensitive": False,
            }
        ],
        "event_types": [
            {
                "event_type": "ProbeContracts.Detected",
                "version": 1,
                "description": "A probe detection",
                "payload_keys": ["probe_id", "tenant_id"],
            }
        ],
        "metadata": {},
    }
    base.update(overrides)
    return base


def test_extension_register__happy_path__installs(
    sql_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin registers a versioned module; catalog lists it."""
    # // given a fresh registry + admin app
    app = _build_extensions_app(sql_session_factory, fresh_registry=True, monkeypatch=monkeypatch)
    # // when registering the probe module
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/extensions",
            json=_extension_body(),
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 201, response.text
        assert response.json()["module_id"] == "ext.probe-contracts"
        # // when listing the catalog
        listed = client.get("/api/v1/extensions")
    # // then the module appears exactly once
    assert listed.status_code == 200, listed.text
    assert [m["module_id"] for m in listed.json()["items"]] == ["ext.probe-contracts"]


def test_extension_register__missing_idempotency_key__rejected(
    sql_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration without Idempotency-Key fails closed with 400."""
    # // given a fresh registry + admin app
    app = _build_extensions_app(sql_session_factory, fresh_registry=True, monkeypatch=monkeypatch)
    # // when posting without the header
    with TestClient(app) as client:
        response = client.post("/api/v1/extensions", json=_extension_body())
    # // then the contract error names the missing header
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "missing_idempotency_key"


def test_extension_register__duplicate_shape__idempotent_conflict_matrix(
    sql_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identical retry is 201; same key with different shape is 409."""
    # // given an installed module
    app = _build_extensions_app(sql_session_factory, fresh_registry=True, monkeypatch=monkeypatch)
    body = _extension_body()
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/extensions", json=body, headers={"Idempotency-Key": str(uuid4())}
        )
        assert first.status_code == 201, first.text
        # // when retrying the identical shape
        retry = client.post(
            "/api/v1/extensions", json=body, headers={"Idempotency-Key": str(uuid4())}
        )
        assert retry.status_code == 201, retry.text
        # // when posting the same key with a different description
        altered = dict(body)
        altered["description"] = "A different description"
        conflict = client.post(
            "/api/v1/extensions", json=altered, headers={"Idempotency-Key": str(uuid4())}
        )
    # // then the retry succeeds and the altered shape conflicts
    assert retry.status_code == 201
    assert conflict.status_code == 409, conflict.text


def test_extension_register__non_admin__denied(
    sql_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workers cannot register modules or read the catalog."""
    # // given a worker app with a fresh registry
    app = _build_extensions_app(
        sql_session_factory, roles=("intake",), fresh_registry=True, monkeypatch=monkeypatch
    )
    # // when the worker registers and lists
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/extensions",
            json=_extension_body(),
            headers={"Idempotency-Key": str(uuid4())},
        )
        listed = client.get("/api/v1/extensions")
    # // then both are 403 forbidden
    assert registered.status_code == 403, registered.text
    assert listed.status_code == 403, listed.text


def test_extension_event_catalog__includes_builtin_and_extension(
    sql_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The union catalog carries built-in types plus the new module type."""
    # // given an installed probe module
    app = _build_extensions_app(sql_session_factory, fresh_registry=True, monkeypatch=monkeypatch)
    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/extensions",
            json=_extension_body(),
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert registered.status_code == 201, registered.text
        # // when reading the union catalog
        catalog = client.get("/api/v1/extensions/events/catalog")
    # // then built-in + extension types both appear (compatibility)
    assert catalog.status_code == 200, catalog.text
    names = {item["event_type"] for item in catalog.json()["items"]}
    assert "CaseCreated" in names
    assert "ProbeContracts.Detected" in names


def test_extension_list__pagination__truncates_window(
    sql_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Module listing honors limit (Page contract, default 50 max 200)."""
    # // given two installed modules
    app = _build_extensions_app(sql_session_factory, fresh_registry=True, monkeypatch=monkeypatch)
    with TestClient(app) as client:
        for module_id in ("ext.probe-alpha", "ext.probe-beta"):
            response = client.post(
                "/api/v1/extensions",
                json=_extension_body(module_id=module_id),
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 201, response.text
        # // when requesting a page of one
        page = client.get("/api/v1/extensions?limit=1")
    # // then exactly one module returns
    assert page.status_code == 200, page.text
    assert len(page.json()["items"]) == 1


def test_admin_outbox_router_module__reexports_router() -> None:
    """The router module keeps its public surface (import smoke)."""
    assert admin_outbox_router_mod.router is outbox_router


__all__ = [name for name in dir() if name.startswith("test_")]
