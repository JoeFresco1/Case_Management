"""FastAPI dependencies: db session, identity, clock, correlation, tenant context.

# INTEGRATION_SEAM: identity — ``LocalHeaderIdentityProvider`` is the local
# adapter; the production seam is an Entra-backed ``IdentityProvider`` that
# the dependency ``get_current_user`` switches to based on ``app_env``.

# INTEGRATION_SEAM: secrets — ``get_secret_provider`` selects the adapter
# declared in ``Settings.secret_provider``; the Azure Key Vault adapter is
# reserved for ``app_env in {"shared", "prod"}`` and is not implemented in
# this slice.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import AppEnv, Settings, get_settings
from app.context import CorrelationContext, bind_actor
from app.db import get_sessionmaker
from app.domain.context import Clock, UserContext
from app.domain.ports import SecretProvider

_MISSING_REQUEST = cast(Request, None)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


def get_clock() -> Clock:
    return SystemClock()


_correlation_header = "x-correlation-id"


def get_correlation_id(
    x_correlation_id: str | None = Header(default=None, alias=_correlation_header),
) -> CorrelationContext:
    cid = x_correlation_id.strip() if x_correlation_id else ""
    if not cid:
        # PyPI name ``uuid7`` ships its module as ``uuid_extensions``
        # (uuid7 was a placeholder during early publishing).
        from uuid_extensions import uuid7  # noqa: PLC0415

        cid = str(uuid7())
    return CorrelationContext(correlation_id=cid)


class IdentityProvider(Protocol):
    def current_user(self) -> UserContext: ...


class LocalHeaderIdentityProvider:
    """Local-dev identity provider.

    Reads `X-Dev-User` and `X-Dev-Tenant` headers. Only honored in `app_env=local`.
    The dependency itself enforces the env check; the provider implementation
    is a pure data source.
    """

    def __init__(
        self, *, user_id: UUID, tenant_id: UUID, display_name: str, roles: tuple[str, ...]
    ) -> None:
        self._user_id = user_id
        self._tenant_id = tenant_id
        self._display_name = display_name
        self._roles = roles

    def current_user(self) -> UserContext:
        return UserContext(
            user_id=self._user_id,
            tenant_id=self._tenant_id,
            display_name=self._display_name,
            roles=self._roles,
        )


# Seeded local dev identities. Real production swaps this for an Entra-backed provider.
_LOCAL_USERS: dict[str, UserContext] = {}


def register_local_user(ctx: UserContext) -> None:
    _LOCAL_USERS[str(ctx.user_id)] = ctx


def clear_local_users() -> None:
    """Clear the local user registry.

    Used by test fixtures to ensure test isolation between test runs.
    """
    _LOCAL_USERS.clear()


def get_current_user(
    settings: Settings = Depends(get_settings),  # noqa: B008
    x_dev_user: str | None = Header(default=None, alias="x-dev-user"),
) -> UserContext:
    if not settings.is_local():
        # Fail-closed: dev-header identity must never be honored outside local.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="dev-header identity is not allowed in this environment",
        )
    if not x_dev_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Dev-User header",
        )
    ctx = _LOCAL_USERS.get(x_dev_user)
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"unknown dev user: {x_dev_user}",
        )
    # why: bind actor + tenant into the structlog contextvars so every
    # log line emitted during the request (including from application/
    # domain layers via the ambient ``structlog.get_logger()``) carries
    # these keys. This is the AGENTS.md §18 contract — every log line
    # MUST include tenant_id, correlation_id, actor_id.
    bind_actor(actor_id=str(ctx.user_id), tenant_id=str(ctx.tenant_id))
    return ctx


async def get_db(request: Request = _MISSING_REQUEST) -> AsyncIterator[AsyncSession]:
    """Share the idempotency transaction when present, otherwise own a session."""
    shared = getattr(request.state, "idempotency_session", None) if request else None
    if shared is not None:
        # The middleware owns commit/rollback so mutation and replay state remain atomic.
        yield cast(AsyncSession, shared)
        return
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Secrets (AGENTS.md §12 — secrets seam)
# ---------------------------------------------------------------------------


def get_secret_provider(
    settings: Settings = Depends(get_settings),  # noqa: B008
    request: Request = _MISSING_REQUEST,
) -> SecretProvider:
    """Resolve the configured ``SecretProvider`` adapter.

    The concrete adapter is composed by ``app.main`` and retrieved from
    ``app.state`` here. Keeping this resolver contract-only prevents HTTP
    modules from acquiring a transitive dependency on infrastructure. Only
    the ``local_env`` adapter is implemented today; selecting
    ``azure_key_vault`` still raises a fail-closed error because the seam is
    reserved for the production target (AGENTS.md §3 Issue #2 — no Azure
    connections possible locally).

    Raises:
        RuntimeError: when the Azure adapter is selected or no provider was
            installed by the composition root.
    """
    if settings.secret_provider == "azure_key_vault":  # noqa: S105
        raise RuntimeError(
            "azure_key_vault secret provider is not implemented in this slice "
            "(AGENTS.md §3 Issue #2: no Azure connections possible locally)"
        )
    if request is _MISSING_REQUEST:
        raise RuntimeError("secret provider is not configured")
    provider = getattr(request.app.state, "secret_provider", None)
    if provider is None:
        raise RuntimeError("secret provider is not configured")
    return cast(SecretProvider, provider)


__all__ = [
    "Clock",
    "SystemClock",
    "CorrelationContext",
    "UserContext",
    "IdentityProvider",
    "LocalHeaderIdentityProvider",
    "AppEnv",
    "get_clock",
    "get_correlation_id",
    "register_local_user",
    "clear_local_users",
    "get_current_user",
    "get_db",
    "get_secret_provider",
    "get_settings",
]
