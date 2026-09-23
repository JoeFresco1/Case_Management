"""Extension registry router (P13-US12 / FM-047).

Endpoints (mounted under ``/api/v1``):

- ``POST   /extensions`` — register a versioned extension module. The
  body is validated end-to-end, the module is installed into the
  process-local ``ExtensionRegistry``, and an ``ExtensionRegistered``
  audit row is appended in the caller's transaction. Idempotent retry
  with identical contents returns ``200`` and ``installed: False``;
  duplicate key with different contents returns ``409``.
- ``GET    /extensions`` — list every installed module for catalog
  inspection (admins / docs).
- ``GET    /extensions/catalog`` — same data, in the published catalog
  shape (matches ``events.py`` router).
- ``GET    /extensions/events/catalog`` — the **union** of the built-in
  event catalog and every extension event type, deduplicated by
  ``(event_type, version)``. Built-in entries win on conflict.

Authorization: every endpoint requires ``ADMIN_TENANT_CONFIG``. The
router calls ``authorize`` before the use case so a denied caller never
reaches the registry or the audit store.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit.http_dependencies import AuditHttpDependencyProvider
from app.application.authorization import Action, Resource, authorize
from app.application.extensions import register_extension as register_extension_uc
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.events import EVENT_CATALOG
from app.domain.extension import (
    EventTypeSpec,
    ExtensionRegistry,
    PolicyActionSpec,
    get_registry,
    iter_event_specs,
)
from app.interfaces.http.dependencies import get_audit_dependencies
from app.interfaces.http.pagination import DEFAULT_LIMIT, MAX_LIMIT
from app.interfaces.http.schemas.extensions import (
    EventTypeOut,
    ExtensionEventCatalog,
    ExtensionOut,
    ExtensionsCatalog,
    RegisterExtensionRequest,
)

router = APIRouter(prefix="/extensions", tags=["extensions"])


# ---------------------------------------------------------------------------
# Conversion helpers — domain -> wire projection
# ---------------------------------------------------------------------------


def _extension_to_out(module) -> ExtensionOut:
    """Project ``RegisteredModule`` to the wire shape.

    Why a local helper: the OpenAPI consumer only needs a stable
    surface; the internal ``RegisteredModule`` carries the audit-event
    correlation id (``installed_at``) but not the actor. Keeping the
    conversion in the router (not the domain) preserves the "domain
    owns no FastAPI types" layering rule.
    """
    spec = module.spec
    return ExtensionOut(
        module_id=spec.module_id,
        version=spec.version,
        description=spec.description,
        author=spec.author,
        policy_actions=[
            {"action": a.action, "description": a.description, "is_sensitive": a.is_sensitive}
            for a in spec.policy_actions
        ],
        event_types=[
            {
                "event_type": e.event_type,
                "version": e.version,
                "description": e.description,
                "payload_keys": list(e.payload_keys),
            }
            for e in spec.event_types
        ],
        installed_at=module.installed_at,
        audit_event_id=module.audit_event_id,
    )


def _registry() -> ExtensionRegistry:
    """Resolve the process-wide registry.

    Why a function: production wiring uses ``get_registry``; tests
    inject a fresh ``ExtensionRegistry`` via ``build_registry`` and
    monkeypatch the import. Keeping the resolution in one place avoids
    the duplicate-import footgun.
    """
    return get_registry()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=ExtensionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a versioned extension module",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or unknown caller identity"},
        status.HTTP_403_FORBIDDEN: {"description": "Caller lacks admin.tenant_config"},
        status.HTTP_409_CONFLICT: {
            "description": "Duplicate (module_id, version) with different shape, "
            "or breaking event-type change"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid module spec"},
    },
)
async def register_extension(
    body: RegisterExtensionRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AuditHttpDependencyProvider = Depends(get_audit_dependencies),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ExtensionOut:
    """Validate, install, and audit-register a module.

    ``Idempotency-Key`` is required (AGENTS.md §10); a retried POST
    with the same key replays the prior response via the middleware.
    Content idempotency also holds: an identical ``(module_id,
    version)`` returns the prior record (``installed=False``
    semantics, status stays ``201``); a duplicate key with a
    different shape raises ``409`` ``conflict`` (no silent
    overwrite). Breaking event-type changes (payload-key removal)
    are likewise ``409`` per the additive-only versioning policy.
    """

    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required for this endpoint",
            },
        )
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="extension"),
    )

    policy_actions = tuple(
        PolicyActionSpec(
            action=p.action,
            description=p.description,
            is_sensitive=p.is_sensitive,
        )
        for p in body.policy_actions
    )
    event_types = tuple(
        EventTypeSpec(
            event_type=e.event_type,
            version=e.version,
            description=e.description,
            payload_keys=tuple(e.payload_keys),
        )
        for e in body.event_types
    )

    audit_store = dependencies.for_session(db).audit
    result = await register_extension_uc.register_extension(
        register_extension_uc.RegisterExtensionCommand(
            tenant_id=user.tenant_id,
            actor=user,
            correlation_id=correlation.correlation_id,
            module_id=body.module_id,
            version=body.version,
            description=body.description,
            policy_actions=policy_actions,
            event_types=event_types,
            author=body.author,
            metadata=body.metadata,
        ),
        registry=_registry(),
        audit=audit_store,
        clock=clock,
    )
    await db.commit()
    return _extension_to_out(result.module)


@router.get(
    "",
    response_model=ExtensionsCatalog,
    summary="List every installed extension module",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or unknown caller identity"},
        status.HTTP_403_FORBIDDEN: {"description": "Caller lacks admin.tenant_config"},
    },
)
async def list_extensions(
    user: UserContext = Depends(get_current_user),
    limit: int = Query(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Maximum modules in the page (default 50, max 200)",
    ),
    offset: int = Query(default=0, ge=0, description="Zero-based offset into install order"),
) -> ExtensionsCatalog:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="extension"),
    )
    modules = _registry().list_modules()
    window = list(modules[offset : offset + limit])
    items = [_extension_to_out(m) for m in window]
    return ExtensionsCatalog(items=items)


@router.get(
    "/catalog",
    response_model=ExtensionsCatalog,
    summary="Catalog of installed extension modules (stable shape)",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or unknown caller identity"},
        status.HTTP_403_FORBIDDEN: {"description": "Caller lacks admin.tenant_config"},
    },
)
async def list_extension_catalog(
    user: UserContext = Depends(get_current_user),
    limit: int = Query(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Maximum modules in the page (default 50, max 200)",
    ),
    offset: int = Query(default=0, ge=0, description="Zero-based offset into install order"),
) -> ExtensionsCatalog:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="extension"),
    )
    modules = _registry().list_modules()
    window = list(modules[offset : offset + limit])
    items = [_extension_to_out(m) for m in window]
    return ExtensionsCatalog(items=items, source="ExtensionRegistry")


@router.get(
    "/events/catalog",
    response_model=ExtensionEventCatalog,
    summary="Union of built-in event catalog and extension event types",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Missing or unknown caller identity"},
        status.HTTP_403_FORBIDDEN: {"description": "Caller lacks admin.tenant_config"},
    },
)
async def list_extension_event_catalog(
    user: UserContext = Depends(get_current_user),
    limit: int = Query(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Maximum event types in the page (default 50, max 200)",
    ),
    offset: int = Query(default=0, ge=0, description="Zero-based offset into catalog order"),
) -> ExtensionEventCatalog:
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="extension"),
    )
    registry = _registry()
    combined = list(iter_event_specs(EVENT_CATALOG, registry.list_event_specs()))
    window = combined[offset : offset + limit]
    items = [
        EventTypeOut(
            event_type=spec.event_type,
            version=spec.version,
            description=spec.description,
            payload_keys=list(spec.payload_keys),
        )
        for spec in window
    ]
    return ExtensionEventCatalog(items=items)


__all__ = ["router"]
