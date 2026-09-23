"""Admin API for the import-mapping registry lifecycle (FM-026 / P14-US3).

Endpoints:

- ``POST   /admin/import-mappings``               — draft a new mapping version.
- ``POST   /admin/import-mappings/{id}/publish``   — publish a draft mapping.
- ``POST   /admin/import-mappings/{id}/retire``   — retire a published mapping.
- ``GET    /admin/import-mappings``               — list all mapping versions for the tenant.
- ``GET    /admin/import-mappings/{id}``          — get one mapping version by id.
- ``GET    /admin/import-mappings/k/{key}``       — list versions for one key.

# Authorization

All endpoints require ``Action.ADMIN_TENANT_CONFIG`` (mirrors the
case-type admin router). Fail-closed: missing role -> ``403 Forbidden``.

# Transaction shape

Each mutation runs inside the same DB transaction as its
``ConfigurationDrafted`` / ``ConfigurationPublished`` /
``ConfigurationRetired`` audit event so the status flip and the audit
row commit atomically (Constitution IV / AGENTS.md §4.R12).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.imports.admin import (
    draft_import_mapping as draft_uc,
)
from app.application.imports.admin import (
    publish_import_mapping as publish_uc,
)
from app.application.imports.admin import (
    retire_import_mapping as retire_uc,
)
from app.application.imports.admin.http_dependencies import (
    AdminImportMappingsHttpDependencyProvider,
)
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.errors import NotFound
from app.interfaces.http.dependencies import get_admin_import_mappings_dependencies
from app.interfaces.http.schemas.admin_import_mappings import (
    DraftImportMappingRequest,
    DraftImportMappingResponse,
    ImportMappingDetailResponse,
    ImportMappingListItem,
)

router = APIRouter(prefix="/admin/import-mappings", tags=["admin", "configuration"])


def _to_detail(m: Any) -> ImportMappingDetailResponse:
    """Project a domain ImportMapping into the detail response."""
    return ImportMappingDetailResponse(
        id=m.id,
        tenant_id=m.tenant_id,
        key=m.key,
        version_number=m.version_number,
        mapping_json=m.mapping_json,
        status=m.status.value,
        published_at=m.published_at.isoformat() if m.published_at else None,
        published_by=m.published_by,
        created_at=m.created_at.isoformat() if m.created_at else "",
    )


def _to_list_item(m: Any) -> ImportMappingListItem:
    """Project a domain ImportMapping into a list item."""
    return ImportMappingListItem(
        id=m.id,
        key=m.key,
        version_number=m.version_number,
        status=m.status.value,
        published_at=m.published_at.isoformat() if m.published_at else None,
        published_by=m.published_by,
        created_at=m.created_at.isoformat() if m.created_at else "",
    )


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=DraftImportMappingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Draft a new import-mapping version (FM-026)",
)
async def draft_import_mapping(
    body: DraftImportMappingRequest,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminImportMappingsHttpDependencyProvider = Depends(
        get_admin_import_mappings_dependencies
    ),
) -> DraftImportMappingResponse:
    """Draft a new import-mapping version under ``body.key``."""
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="import_mapping"),
        role_repository=repositories.role,
    )

    result = await draft_uc(
        draft_uc.DraftImportMappingCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            key=body.key,
            mapping_json=dict(body.mapping_json),
            correlation_id=correlation.correlation_id,
        ),
        repo=repositories.import_mapping,
        audit_store=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return DraftImportMappingResponse(
        mapping_id=result.mapping_id,
        version_number=result.version_number,
        status=result.status,
    )


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


@router.post(
    "/{mapping_id}/publish",
    response_model=DraftImportMappingResponse,
    status_code=status.HTTP_200_OK,
    summary="Publish a draft import-mapping version (FM-026)",
)
async def publish_import_mapping(
    mapping_id: UUID = Path(...),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminImportMappingsHttpDependencyProvider = Depends(
        get_admin_import_mappings_dependencies
    ),
) -> DraftImportMappingResponse:
    """Publish a draft import-mapping version. Idempotent on re-publish."""
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="import_mapping"),
        role_repository=repositories.role,
    )

    result = await publish_uc(
        publish_uc.PublishImportMappingCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            mapping_id=mapping_id,
            correlation_id=correlation.correlation_id,
        ),
        repo=repositories.import_mapping,
        audit_store=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return DraftImportMappingResponse(
        mapping_id=result.mapping_id,
        version_number=result.version_number,
        status=result.status,
    )


# ---------------------------------------------------------------------------
# Retire
# ---------------------------------------------------------------------------


@router.post(
    "/{mapping_id}/retire",
    response_model=DraftImportMappingResponse,
    status_code=status.HTTP_200_OK,
    summary="Retire a published import-mapping version (FM-026)",
)
async def retire_import_mapping_endpoint(
    mapping_id: UUID = Path(...),
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminImportMappingsHttpDependencyProvider = Depends(
        get_admin_import_mappings_dependencies
    ),
) -> DraftImportMappingResponse:
    """Retire a published import-mapping version.

    Retired mappings are no longer eligible to back new jobs, but old
    jobs keep their inline snapshot (Constitution XI).
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="import_mapping"),
        role_repository=repositories.role,
    )

    result = await retire_uc(
        retire_uc.RetireImportMappingCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            mapping_id=mapping_id,
            correlation_id=correlation.correlation_id,
        ),
        repo=repositories.import_mapping,
        audit_store=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return DraftImportMappingResponse(
        mapping_id=result.mapping_id,
        version_number=result.version_number,
        status=result.status,
    )


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[ImportMappingListItem],
    summary="List all import-mapping versions for the tenant (FM-026)",
)
async def list_import_mappings(
    key: str | None = Query(default=None, max_length=64),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminImportMappingsHttpDependencyProvider = Depends(
        get_admin_import_mappings_dependencies
    ),
) -> list[ImportMappingListItem]:
    """List import-mapping versions.

    When ``key`` is supplied, only versions for that key are returned
    (ordered by version number, ascending). Otherwise all versions for
    the tenant are returned (newest first by created_at).
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="import_mapping"),
        role_repository=repositories.role,
    )

    if key is not None:
        versions = await repositories.import_mapping.list_for_key(user.tenant_id, key)
    else:
        versions = await repositories.import_mapping.list_for_tenant(user.tenant_id)
    return [_to_list_item(m) for m in versions]


@router.get(
    "/{mapping_id}",
    response_model=ImportMappingDetailResponse,
    summary="Get an import-mapping version by id (FM-026)",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Import mapping not found"},
    },
)
async def get_import_mapping(
    mapping_id: UUID = Path(...),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminImportMappingsHttpDependencyProvider = Depends(
        get_admin_import_mappings_dependencies
    ),
) -> ImportMappingDetailResponse:
    """Return the full detail of one import-mapping version."""
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="import_mapping"),
        role_repository=repositories.role,
    )

    mapping = await repositories.import_mapping.get(user.tenant_id, mapping_id)
    if mapping is None:
        raise NotFound(f"import mapping {mapping_id} not found")
    return _to_detail(mapping)


__all__ = ["router"]
