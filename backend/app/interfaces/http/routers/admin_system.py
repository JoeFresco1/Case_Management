"""System configuration admin API (T7 diagnostics + system utilities / P10).

Endpoints (all behind the ``System Configuration`` right, wired as
``Action.ADMIN_TENANT_CONFIG`` — fail-closed, deny by default):

- ``GET /api/v1/admin/system/settings`` — list tenant settings
  (module log settings, cache/service diagnostics config, storage
  volume declaration, encryption-at-rest declaration). Values are
  secret-redacted; each row carries its ``version`` for optimistic
  concurrency.
- ``GET /api/v1/admin/system/settings/{key}`` — one redacted setting.
- ``PUT /api/v1/admin/system/settings/{key}`` — validate + append a new
  version. Body carries ``expected_version`` (stale → 409 ``conflict``,
  T7-US1 concurrent-edit protection) and ``change_summary`` (audited).
- ``GET /api/v1/admin/system/diagnostics`` — cache stats + desired vs
  actual background-service status + synthetic marker.
- ``POST /api/v1/admin/system/cache/clear`` — cache utility (returns
  entries cleared; the mutation is audit-logged).
- ``GET /api/v1/admin/system/storage`` — declared volume/encryption
  config (from the settings store) alongside the *actual* adapter view
  (from the infrastructure adapters wired on ``app.state``).

# Layering

This module imports application + config only — never infrastructure
(import-linter contract ``Interfaces do not import infrastructure
directly``). The storage/encryption/diagnostics adapters are resolved
duck-typed off ``request.app.state`` (wired by the composition root in
``app.main``); when unwired (minimal test apps) the router degrades to
settings-derived views so the surface stays testable without a container.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.application.authorization import Action, Resource, authorize
from app.application.configuration import system_settings as sysconf
from app.config import Settings, get_settings
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
)

router = APIRouter(prefix="/admin/system", tags=["admin", "system"])

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Schemas (inline — the surface is small; no separate schemas module needed)
# ---------------------------------------------------------------------------


class SettingResponse(BaseModel):
    """One redacted setting with its optimistic-concurrency version."""

    key: str
    value: dict[str, Any]
    version: int
    updated_by: UUID
    updated_at: str
    change_summary: str


class SettingListResponse(BaseModel):
    """Tenant settings list (redacted), sorted by key."""

    items: list[SettingResponse]


class UpdateSettingRequest(BaseModel):
    """Body for ``PUT /admin/system/settings/{key}``."""

    value: dict[str, Any]
    expected_version: int = Field(ge=1)
    change_summary: str = Field(default="", max_length=4000)


class UpdateSettingResponse(BaseModel):
    """New version plus the redacted audit event type for the history view."""

    key: str
    value: dict[str, Any]
    version: int
    audit_event: str


class DiagnosticsResponse(BaseModel):
    """Cache stats + desired vs actual service status."""

    cache: dict[str, Any]
    services_desired: dict[str, Any]
    services_actual: dict[str, Any]
    synthetic_environment: bool


class CacheClearResponse(BaseModel):
    """Cache utility outcome (the mutation itself is audit-logged)."""

    cleared: int


class StorageResponse(BaseModel):
    """Declared config vs actual adapter view (never leaks key content)."""

    declared_volume: dict[str, Any]
    declared_encryption: dict[str, Any]
    actual_volume: dict[str, Any]
    actual_encryption: dict[str, Any]


# ---------------------------------------------------------------------------
# State resolution (duck-typed — no infrastructure imports allowed here)
# ---------------------------------------------------------------------------


def _store(request: Request) -> sysconf.SystemSettingsStore:
    """Resolve the versioned settings store (per-app, tenant-isolated)."""
    store = getattr(request.app.state, "system_settings_store", None)
    if store is None:
        # why: minimal test apps mount this router without the full
        # composition root; a per-app in-memory store keeps those tests
        # hermetic without importing infrastructure (layer contract).
        store = sysconf.InMemorySystemSettingsStore()
        request.app.state.system_settings_store = store
    return store


def _diagnostics_view(request: Request) -> dict[str, Any]:
    """Cache stats from the wired service, else an empty-cache view."""
    service = getattr(request.app.state, "system_diagnostics", None)
    stats = getattr(service, "stats", None)
    if callable(stats):
        raw = stats()
        if isinstance(raw, dict):
            return {str(k): v for k, v in raw.items()}
    return {"entries": 0, "max_entries": 1000, "ttl_seconds": 300}


def _clear_diagnostics_cache(request: Request) -> int:
    """Clear the wired diagnostics cache; 0 when unwired (nothing cached)."""
    service = getattr(request.app.state, "system_diagnostics", None)
    clear = getattr(service, "clear", None)
    if callable(clear):
        cleared = clear()
        if isinstance(cleared, int):
            return cleared
    return 0


def _service_actual(request: Request, settings: Settings) -> dict[str, Any]:
    """Actual background-service flags (adapter when wired, else Settings)."""
    service = getattr(request.app.state, "system_diagnostics", None)
    status = getattr(service, "service_status", None)
    if callable(status):
        raw = status(
            outbox_enabled=settings.outbox_dispatcher_enabled,
            sla_scanner_enabled=settings.sla_scanner_enabled,
            webhook_dispatcher_enabled=settings.webhook_dispatcher_enabled,
            synthetic_environment=settings.synthetic_environment,
        )
        if isinstance(raw, dict):
            return {str(k): v for k, v in raw.items()}
    return {
        "outbox": settings.outbox_dispatcher_enabled,
        "sla_scanner": settings.sla_scanner_enabled,
        "webhook_dispatcher": settings.webhook_dispatcher_enabled,
        "synthetic_environment": settings.synthetic_environment,
    }


def _actual_volume(request: Request, settings: Settings) -> dict[str, Any]:
    """Actual volume view (adapter when wired, else settings-derived)."""
    volume = getattr(request.app.state, "storage_volume", None)
    describe = getattr(volume, "describe", None)
    if callable(describe):
        raw = describe()
        if isinstance(raw, dict):
            return {str(k): v for k, v in raw.items()}
    return {
        "adapter": "local-fs",
        "root": str(settings.documents_path),
        "cache_mb": 256,
        "tier": "standard",
        "free_bytes": None,
    }


def _actual_encryption(request: Request) -> dict[str, Any]:
    """Actual encryption posture (adapter when wired, else plaintext)."""
    adapter = getattr(request.app.state, "encryption_at_rest", None)
    describe = getattr(adapter, "describe", None)
    if callable(describe):
        raw = describe()
        if isinstance(raw, dict):
            return {str(k): v for k, v in raw.items()}
    return {"adapter": "local-plaintext", "enabled": False, "key_ref": ""}


def _to_response(setting: sysconf.SystemSetting) -> SettingResponse:
    """Map an application setting to the redacted wire shape."""
    return SettingResponse(
        key=setting.key,
        value=dict(setting.value),
        version=setting.version,
        updated_by=setting.updated_by,
        updated_at=setting.updated_at.isoformat(),
        change_summary=setting.change_summary,
    )


async def _authorize(user: UserContext) -> None:
    """Enforce the System Configuration right (fail-closed)."""
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="configuration"),
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get(
    "/settings",
    response_model=SettingListResponse,
    summary="List system settings (T7, redacted)",
)
async def list_system_settings(
    request: Request,
    user: UserContext = Depends(get_current_user),
    clock: Clock = Depends(get_clock),
) -> SettingListResponse:
    """List every tenant setting (redacted copy, sorted by key)."""
    await _authorize(user)
    store = _store(request)
    await sysconf.ensure_defaults(user.tenant_id, store=store, actor_id=user.user_id, clock=clock)
    settings = await sysconf.list_settings(user.tenant_id, store=store)
    return SettingListResponse(items=[_to_response(s) for s in settings])


@router.get(
    "/settings/{key}",
    response_model=SettingResponse,
    summary="Get one system setting (T7, redacted)",
)
async def get_system_setting(
    key: str,
    request: Request,
    user: UserContext = Depends(get_current_user),
) -> SettingResponse:
    """Return one redacted setting; unknown key is 404 (tenant-scoped)."""
    await _authorize(user)
    setting = await sysconf.get_setting(user.tenant_id, key, store=_store(request))
    return _to_response(setting)


@router.put(
    "/settings/{key}",
    response_model=UpdateSettingResponse,
    summary="Update a system setting (T7, versioned + audited)",
)
async def update_system_setting(
    key: str,
    body: UpdateSettingRequest,
    request: Request,
    user: UserContext = Depends(get_current_user),
    clock: Clock = Depends(get_clock),
    correlation: CorrelationContext = Depends(get_correlation_id),
) -> UpdateSettingResponse:
    """Validate + append a new version (stale ``expected_version`` → 409)."""
    await _authorize(user)
    store = _store(request)
    # why: seed here as well as on reads — a fresh tenant's first
    # write must land on the version-1 defaults (idempotent; customized
    # values are never clobbered) instead of 404ing.
    await sysconf.ensure_defaults(user.tenant_id, store=store, actor_id=user.user_id, clock=clock)
    result = await sysconf.update_setting(
        sysconf.UpdateSettingCommand(
            tenant_id=user.tenant_id,
            key=key,
            value=dict(body.value),
            expected_version=body.expected_version,
            actor_id=user.user_id,
            change_summary=body.change_summary,
            correlation_id=correlation.correlation_id,
        ),
        store=store,
        clock=clock,
    )
    # why: the audit payload value is already redacted in the use case;
    # logging it here gives operators the who/when/what without ever
    # risking a secret in the log line (Constitution V, FM-011).
    log.info(
        "admin.system.setting_updated",
        tenant_id=str(user.tenant_id),
        actor_id=str(user.user_id),
        key=key,
        version=result.setting.version,
        value=result.audit.redacted_value,
        change_summary=body.change_summary,
        correlation_id=correlation.correlation_id,
    )
    return UpdateSettingResponse(
        key=key,
        value=dict(result.audit.redacted_value),
        version=result.setting.version,
        audit_event=result.audit.event_type,
    )


# ---------------------------------------------------------------------------
# Diagnostics + cache utility
# ---------------------------------------------------------------------------


@router.get(
    "/diagnostics",
    response_model=DiagnosticsResponse,
    summary="Cache/service diagnostics (T7)",
)
async def get_diagnostics(
    request: Request,
    user: UserContext = Depends(get_current_user),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings),
) -> DiagnosticsResponse:
    """Cache stats plus desired (tenant config) vs actual (process) services."""
    await _authorize(user)
    store = _store(request)
    await sysconf.ensure_defaults(user.tenant_id, store=store, actor_id=user.user_id, clock=clock)
    desired = await sysconf.get_setting(user.tenant_id, "diagnostics.services", store=store)
    return DiagnosticsResponse(
        cache=_diagnostics_view(request),
        services_desired=dict(desired.value),
        services_actual=_service_actual(request, settings),
        synthetic_environment=settings.synthetic_environment,
    )


@router.post(
    "/cache/clear",
    response_model=CacheClearResponse,
    summary="Clear the diagnostics cache (T7 utility)",
)
async def clear_cache(
    request: Request,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
) -> CacheClearResponse:
    """Drop all diagnostics-cache entries (audit-logged, tenant right held)."""
    await _authorize(user)
    cleared = _clear_diagnostics_cache(request)
    log.info(
        "admin.system.cache_cleared",
        tenant_id=str(user.tenant_id),
        actor_id=str(user.user_id),
        cleared=cleared,
        correlation_id=correlation.correlation_id,
    )
    return CacheClearResponse(cleared=cleared)


# ---------------------------------------------------------------------------
# Storage + encryption view
# ---------------------------------------------------------------------------


@router.get(
    "/storage",
    response_model=StorageResponse,
    summary="Storage volume + encryption-at-rest view (T7)",
)
async def get_storage(
    request: Request,
    user: UserContext = Depends(get_current_user),
    clock: Clock = Depends(get_clock),
    settings: Settings = Depends(get_settings),
) -> StorageResponse:
    """Declared tenant config alongside the actual infrastructure view."""
    await _authorize(user)
    store = _store(request)
    await sysconf.ensure_defaults(user.tenant_id, store=store, actor_id=user.user_id, clock=clock)
    volume = await sysconf.get_setting(user.tenant_id, "storage.volume", store=store)
    encryption = await sysconf.get_setting(user.tenant_id, "encryption.at_rest", store=store)
    return StorageResponse(
        declared_volume=dict(volume.value),
        declared_encryption=dict(encryption.value),
        actual_volume=_actual_volume(request, settings),
        actual_encryption=_actual_encryption(request),
    )


__all__ = ["router"]
