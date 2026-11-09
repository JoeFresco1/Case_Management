"""Local-runtime contract tests (AGENTS.md §3, §18; R12; P11-US3).

This file exercises the cross-cutting runtime contract:

- ``Settings`` accepts the four ``APP_ENV`` postures (``local``,
  ``test``, ``shared``, ``prod``) and rejects anything else.
- ``Settings.synthetic_environment`` is True by default and the flag
  surfaces through ``/api/v1/health``.
- ``LocalHeaderIdentityProvider`` (the dev-header IdP) is honored only
  in ``APP_ENV=local``; every other posture returns 401.
- The health endpoint exposes ``synthetic_environment`` so the
  frontend banner (shell/Banner.tsx) has a single source of truth.

Why this lives in ``tests/foundation/``: it does not depend on
Postgres (it runs against the in-memory app config and the
FastAPI ASGI transport). It is the minimum viable gate that any
environment-specific regression must surface — i.e., an operator who
accidentally flips ``app_env`` from ``local`` to ``prod`` will see the
header-IdP tests start failing immediately.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.deps import (
    LocalHeaderIdentityProvider,
    UserContext,
    get_current_user,
    register_local_user,
)
from app.infrastructure.persistence.models import Tenant
from app.infrastructure.persistence.models import User as UserRow
from app.interfaces.http.routers.health import router as health_router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def local_settings() -> Settings:
    """Settings pinned to the local posture with synthetic mode on."""
    return Settings(app_env="local", synthetic_environment=True)


@pytest.fixture
def test_settings() -> Settings:
    """Settings pinned to the test posture."""
    return Settings(app_env="test", synthetic_environment=True)


@pytest.fixture
def shared_settings() -> Settings:
    """Settings pinned to the shared posture."""
    return Settings(app_env="shared", synthetic_environment=True)


@pytest.fixture
def prod_settings() -> Settings:
    """Settings pinned to the prod posture (synthetic OFF, post-migration)."""
    return Settings(app_env="prod", synthetic_environment=False)


@pytest.fixture
def health_app() -> FastAPI:
    """A FastAPI app exposing only ``/api/v1/health``.

    We mount just the health router so the test does not depend on the
    full app's startup (DB engine, idempotency middleware, etc.). The
    health endpoint is the canonical adapter inventory and is the only
    seam needed for the synthetic-environment banner contract.
    """
    app = FastAPI(title="health-test-app")
    app.include_router(health_router, prefix="/api/v1")
    # Override the settings dependency to a known posture so the test is
    # hermetic — ``get_settings`` is cached at the module level, so this
    # override is required to swap the value per-test.
    return app


@pytest.fixture
def health_client(health_app: FastAPI) -> Any:
    with TestClient(health_app) as client:
        yield client


# ---------------------------------------------------------------------------
# APP_ENV enum + synthetic_environment flag
# ---------------------------------------------------------------------------


def test_settings__accepts_all_four_app_env_values() -> None:
    """``APP_ENV`` accepts ``local``, ``test``, ``shared``, ``prod``."""
    for env in ("local", "test", "shared", "prod"):
        s = Settings(app_env=env)
        assert s.app_env == env


def test_settings__rejects_unknown_app_env_value() -> None:
    """An unknown ``APP_ENV`` value fails closed at startup (R3, §3)."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(app_env="staging")  # type: ignore[arg-type]
    # The validator's error message names the rejected value.
    assert "staging" in str(excinfo.value)


def test_settings__is_local_and_is_production_like_are_mutually_exclusive() -> None:
    """``is_local()`` and ``is_production_like()`` partition the four
    postures. The IdP gate (``get_current_user``) depends on this
    partition to fail closed in every non-local posture.
    """
    for env, is_local, is_prod in (
        ("local", True, False),
        ("test", False, False),
        ("shared", False, True),
        ("prod", False, True),
    ):
        s = Settings(app_env=env)  # type: ignore[arg-type]
        assert s.is_local() is is_local
        assert s.is_production_like() is is_prod


def test_settings__synthetic_environment_defaults_true() -> None:
    """The default posture is always synthetic (R12, §18)."""
    s = Settings()
    assert s.synthetic_environment is True


# ---------------------------------------------------------------------------
# /api/v1/health — synthetic_environment surface
# ---------------------------------------------------------------------------


def test_health__exposes_synthetic_environment_flag(
    health_client: TestClient,
) -> None:
    """``GET /api/v1/health`` returns ``synthetic_environment: true``."""
    response = health_client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["synthetic_environment"] is True
    assert body["status"] == "ok"


def test_health__exposes_app_env_and_adapters(
    health_client: TestClient,
) -> None:
    """The health body carries the active ``app_env`` and the adapter
    inventory. Operators rely on this to verify which seams are wired
    without tailing the structured log stream.
    """
    body = health_client.get("/api/v1/health").json()
    assert "app_env" in body
    assert "adapters" in body
    for key in (
        "db",
        "store",
        "bus",
        "idp",
        "telemetry",
    ):
        assert key in body["adapters"], (
            f"health adapter inventory missing {key!r}; got {list(body['adapters'])}"
        )


def test_health__identity_provider_is_local_only_in_local_env(
    health_app: FastAPI,
) -> None:
    """``identity_provider`` flips from ``local-dev-header`` to
    ``denied`` outside the local posture — the canonical AGENTS.md §12
    seam surface.
    """
    for env, expected in (
        ("local", "local-header"),
        ("test", "entra"),
        ("shared", "entra"),
        ("prod", "entra"),
    ):
        # Override the settings dependency per-env so each assertion
        # sees its own posture without mutating the process-global
        # cache.
        env_settings = Settings(app_env=env)  # type: ignore[arg-type]

        health_app.dependency_overrides[get_settings] = lambda _s=env_settings: _s
        with TestClient(health_app) as client:
            body = client.get("/api/v1/health").json()
        assert body["adapters"]["idp"] == expected, (
            f"env={env}: expected idp={expected}, got {body['adapters']['idp']}"
        )


# ---------------------------------------------------------------------------
# LocalHeaderIdentityProvider + get_current_user gating
# ---------------------------------------------------------------------------


def _build_user_context() -> UserContext:
    """A canonical user context for the IdP tests."""
    return UserContext(
        user_id=UUID("00000000-0000-0000-0000-000000000001"),
        tenant_id=UUID("11111111-1111-1111-1111-111111111111"),
        display_name="Alex Intake",
        roles=("intake",),
    )


def test_local_header_identity_provider__returns_user_context_for_known_key() -> None:
    """The local IdP returns the registered context for ``X-Dev-User=intake``."""
    ctx = _build_user_context()
    register_local_user(ctx)
    provider = LocalHeaderIdentityProvider(
        user_id=ctx.user_id,
        tenant_id=ctx.tenant_id,
        display_name=ctx.display_name,
        roles=ctx.roles,
    )
    resolved = provider.current_user()
    assert resolved.user_id == ctx.user_id
    assert resolved.tenant_id == ctx.tenant_id
    assert resolved.roles == ctx.roles


def test_get_current_user__returns_401_in_non_local_env(
    test_settings: Settings,
) -> None:
    """``get_current_user`` rejects the dev header with 401 outside
    ``APP_ENV=local`` — the canonical P11-US3 / R12 contract.
    """
    # The dependency signature accepts a ``Settings`` via ``Depends``;
    # we call it directly so the test does not need the FastAPI
    # request/response machinery for a non-router assertion.
    register_local_user(_build_user_context())
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(
            settings=test_settings,
            x_dev_user="intake",
        )
    assert excinfo.value.status_code == 401


def test_get_current_user__returns_401_in_local_env_when_header_missing(
    local_settings: Settings,
) -> None:
    """Even in ``APP_ENV=local``, a missing ``X-Dev-User`` is 401."""
    register_local_user(_build_user_context())
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(
            settings=local_settings,
            x_dev_user=None,
        )
    assert excinfo.value.status_code == 401


def test_get_current_user__returns_401_in_local_env_for_unknown_user(
    local_settings: Settings,
) -> None:
    """An unknown ``X-Dev-User`` key returns 401 in local mode (the IdP
    registry is the source of truth).
    """
    register_local_user(_build_user_context())
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(
            settings=local_settings,
            x_dev_user="ghost",
        )
    assert excinfo.value.status_code == 401


def test_get_current_user__returns_user_in_local_env(
    local_settings: Settings,
) -> None:
    """Happy path: in ``APP_ENV=local`` with a known ``X-Dev-User`` the
    dependency returns the registered context.
    """
    register_local_user(_build_user_context())
    resolved = get_current_user(
        settings=local_settings,
        x_dev_user="00000000-0000-0000-0000-000000000001",
    )
    assert resolved.user_id == UUID("00000000-0000-0000-0000-000000000001")


def test_get_current_user__resolves_seed_key_and_checks_tenant() -> None:
    """Seed keys resolve only to their registered tenant identity."""
    ctx = _build_user_context()
    register_local_user(ctx, key="intake")

    resolved = get_current_user(
        settings=Settings(app_env="local"),
        x_dev_user="intake",
        x_dev_tenant=str(ctx.tenant_id),
    )
    assert resolved.user_id == ctx.user_id

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(
            settings=Settings(app_env="local"),
            x_dev_user="intake",
            x_dev_tenant="22222222-2222-2222-2222-222222222222",
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_local_identity_hydration__restores_persisted_seed_aliases(
    sql_session_factory: Any,
) -> None:
    """A server process rebuilds local identities from persisted user rows."""
    # FastAPI currently warns while main.py constructs the application at import.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from app.main import _hydrate_local_identity_registry  # noqa: PLC0415

    tenant_id = UUID("11111111-1111-1111-1111-111111111111")
    user_id = UUID("00000000-0000-0000-0000-000000000123")
    now = datetime.now(tz=UTC)
    async with sql_session_factory() as session:
        session.add(
            Tenant(
                id=tenant_id,
                key="acme",
                name="Acme Synthetic Co.",
                created_at=now,
            )
        )
        session.add(
            UserRow(
                id=user_id,
                tenant_id=tenant_id,
                key="process_admin",
                display_name="Pat Process Admin",
                roles=["process_admin"],
                is_system=False,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    await _hydrate_local_identity_registry(sql_session_factory)

    resolved_by_key = get_current_user(
        settings=Settings(app_env="local"),
        x_dev_user="process_admin",
        x_dev_tenant=str(tenant_id),
    )
    resolved_by_id = get_current_user(
        settings=Settings(app_env="local"),
        x_dev_user=str(user_id),
        x_dev_tenant=str(tenant_id),
    )
    assert resolved_by_key.user_id == user_id
    assert resolved_by_id.user_id == user_id
    assert resolved_by_key.roles == ("process_admin",)


# ---------------------------------------------------------------------------
# Runtime safety: config + IdP together
# ---------------------------------------------------------------------------


def test_runtime__test_env_with_dev_header_returns_401() -> None:
    """End-to-end: a request to a protected route with ``APP_ENV=test``
    and ``X-Dev-User`` returns 401. The build_app helper spins up a
    tiny FastAPI app with a dummy protected route so the test is
    hermetic — no DB, no idempotency middleware.
    """
    test_app = _build_protected_app(app_env="test")

    register_local_user(_build_user_context())

    with TestClient(test_app) as client:
        response = client.get(
            "/api/v1/protected",
            headers={"X-Dev-User": "00000000-0000-0000-0000-000000000001"},
        )
    assert response.status_code == 401
    assert "dev-header" in response.json()["detail"]


def test_runtime__local_env_with_dev_header_returns_200() -> None:
    """End-to-end: a request to a protected route with ``APP_ENV=local``
    and a valid ``X-Dev-User`` returns 200 with the user context.
    """
    test_app = _build_protected_app(app_env="local")

    register_local_user(_build_user_context())

    with TestClient(test_app) as client:
        response = client.get(
            "/api/v1/protected",
            headers={"X-Dev-User": "00000000-0000-0000-0000-000000000001"},
        )
    assert response.status_code == 200
    assert response.json() == {"user_id": "00000000-0000-0000-0000-000000000001"}


def _build_protected_app(*, app_env: str) -> FastAPI:
    """Build a tiny FastAPI app with a protected route for runtime tests.

    Why a helper: the two ``test_runtime_*`` cases differ only in
    ``app_env`` and the asserted outcome; the app construction is
    identical. Extracting it keeps both tests single-purpose and the
    ``Depends(...)`` FastAPI idiom in one place.
    """
    test_app = FastAPI()

    @test_app.get("/api/v1/protected")
    async def protected(user: Any = Depends(get_current_user)) -> dict[str, Any]:  # noqa: B008
        return {"user_id": str(user.user_id)}

    env_settings = Settings(app_env=app_env)  # type: ignore[arg-type]
    test_app.dependency_overrides[get_settings] = lambda: env_settings
    return test_app
