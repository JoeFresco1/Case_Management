"""Admin API for case-type configuration lifecycle (FM-008 / FR-015, FR-033).

Endpoints:

- ``POST /api/v1/admin/case-types/{key}/versions`` — draft a new version
  of an existing CaseType. Returns the new draft id and version number.
- ``POST /api/v1/admin/case-types/{key}/versions/{version_id}/publish`` —
  validate + publish the draft. Idempotent: re-publishing a published
  version returns its existing identity without re-running the
  validation.
- ``GET /api/v1/admin/case-types/{key}/versions`` — publication history
  with status, publish actor, effective time, and schema fingerprint.

# Authorization

All endpoints require the ``ADMIN_TENANT_CONFIG`` action; only the
``process_admin`` role grants it (app/application/authorization.py).
Fail-closed: missing role -> ``403 Forbidden``.

# Transaction shape

Draft and publish both run inside the same DB transaction as their
``ConfigurationDrafted`` / ``ConfigurationPublished`` audit event so the
status flip, the current-version pointer, and the audit row commit
atomically (Constitution IV / plan §4.R12). The HTTP boundary commits
once after the use case returns.

# Idempotency

The publish endpoint accepts the standard ``Idempotency-Key`` header.
Replays of a successful publish return the original result; a
same-key + different-request-hash returns 409 (handled by
``interfaces/http/idempotency.py`` when that middleware lands — until
then we treat duplicate ``POST`` as a normal request and rely on the
domain immutability guard ``InvariantViolation`` on a non-draft version).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.configuration import draft_case_type, publish_case_type
from app.application.configuration.http_dependencies import (
    AdminCaseTypesHttpDependencyProvider,
)
from app.application.configuration.publication_history import (
    PublicationHistoryEntry,
    PublicationHistoryQuery,
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
from app.domain.case_type import CaseTypeSchema
from app.domain.errors import NotFound
from app.interfaces.http.dependencies import get_admin_case_types_dependencies
from app.interfaces.http.schemas.admin_case_types import (
    CaseTypeSchemaRequest,
    CaseTypeVersionDetail,
    CaseTypeVersionSummary,
    DraftCaseTypeVersionRequest,
    DraftResultResponse,
    FieldDefinitionRequest,
    PublicationHistoryResponse,
    PublishResultResponse,
)

router = APIRouter(prefix="/admin/case-types", tags=["admin", "configuration"])


def _to_history(entry: PublicationHistoryEntry) -> CaseTypeVersionSummary:
    return CaseTypeVersionSummary(
        id=entry.id,
        version_number=entry.version_number,
        status=entry.status.value,
        published_at=entry.published_at.isoformat() if entry.published_at else None,
        published_by=entry.published_by,
        created_at=entry.created_at.isoformat(),
        field_keys=list(entry.field_keys),
        document_classifications=list(entry.document_classifications),
    )


def _to_detail(*, case_type_id: UUID, version) -> CaseTypeVersionDetail:
    """Project one domain version to the inspect wire shape.

    Why here and not in the use case: the inspect read is a thin
    tenant-scoped projection (no lifecycle transition, no audit
    write). The router already owns the summary projection
    (``_to_history``); the detail projection stays beside it so the
    history and inspect shapes cannot drift apart unnoticed.
    """
    schema = version.schema
    return CaseTypeVersionDetail(
        id=version.id,
        case_type_id=case_type_id,
        version_number=version.version_number,
        status=version.status.value,
        published_at=version.published_at.isoformat() if version.published_at else None,
        published_by=version.published_by,
        created_at=version.created_at.isoformat() if version.created_at else "",
        field_keys=list(schema.field_keys()),
        document_classifications=list(schema.document_classifications),
        schema=CaseTypeSchemaRequest(
            fields=[
                FieldDefinitionRequest(
                    key=f.key,
                    label=f.label,
                    type=f.type,  # type: ignore[arg-type]
                    required=f.required,
                    enum_values=list(f.enum_values),
                    sensitivity=f.sensitivity,  # type: ignore[arg-type]
                    searchable=f.searchable,
                )
                for f in schema.fields
            ],
            document_classifications=list(schema.document_classifications),
            rules=[dict(r) for r in schema.rules],
        ),
    )


@router.post(
    "/{case_type_key}/versions",
    response_model=DraftResultResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Draft a new version of a CaseType",
)
async def draft_case_type_version(
    case_type_key: str = Path(min_length=1, max_length=64),
    body: DraftCaseTypeVersionRequest = ...,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminCaseTypesHttpDependencyProvider = Depends(get_admin_case_types_dependencies),
) -> DraftResultResponse:
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="case_type"),
        role_repository=repositories.role,
    )

    schema = CaseTypeSchema(
        fields=_to_domain_fields(body),
        document_classifications=tuple(body.schema_.document_classifications),
        rules=tuple(body.schema_.rules),
    )

    result = await draft_case_type.handle(
        draft_case_type.DraftCaseTypeVersionCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            case_type_key=case_type_key,
            schema=schema,
            correlation_id=correlation.correlation_id,
        ),
        case_type_repo=repositories.case_type,
        case_type_version_repo=repositories.case_type_version,
        audit_store=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return DraftResultResponse(
        case_type_id=result.case_type_id,
        case_type_version_id=result.case_type_version_id,
        version_number=result.version_number,
        status="draft",
    )


def _to_domain_fields(
    body: DraftCaseTypeVersionRequest,
) -> tuple:
    from app.domain.case_type import CaseTypeField  # noqa: PLC0415

    return tuple(
        CaseTypeField(
            key=f.key,
            label=f.label,
            type=f.type,
            required=f.required,
            enum_values=tuple(f.enum_values),
            sensitivity=f.sensitivity,
            searchable=f.searchable,
        )
        for f in body.schema_.fields
    )


@router.post(
    "/{case_type_key}/versions/{version_id}/publish",
    response_model=PublishResultResponse,
    status_code=status.HTTP_200_OK,
    summary="Validate and publish a draft CaseType version",
)
async def publish_case_type_version(
    case_type_key: str = Path(min_length=1, max_length=64),  # noqa: ARG001
    version_id: UUID = ...,
    user: UserContext = Depends(get_current_user),
    correlation: CorrelationContext = Depends(get_correlation_id),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: AdminCaseTypesHttpDependencyProvider = Depends(get_admin_case_types_dependencies),
) -> PublishResultResponse:
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="case_type"),
        role_repository=repositories.role,
    )

    result = await publish_case_type.handle(
        publish_case_type.PublishCaseTypeVersionCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            case_type_version_id=version_id,
            correlation_id=correlation.correlation_id,
        ),
        case_type_repo=repositories.case_type,
        case_type_version_repo=repositories.case_type_version,
        audit_store=repositories.audit,
        clock=clock,
    )
    await db.commit()
    return PublishResultResponse(
        case_type_id=result.case_type_id,
        case_type_version_id=result.case_type_version_id,
        version_number=result.version_number,
        status=result.status.value,
    )


@router.get(
    "/{case_type_key}/versions",
    response_model=PublicationHistoryResponse,
    summary="List publication history for a CaseType",
)
async def list_case_type_versions(
    case_type_key: str = Path(min_length=1, max_length=64),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminCaseTypesHttpDependencyProvider = Depends(get_admin_case_types_dependencies),
) -> PublicationHistoryResponse:
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="case_type"),
        role_repository=repositories.role,
    )

    # Resolve the key to an id (the history use case is id-keyed).
    case_type = await repositories.case_type.get_by_key(user.tenant_id, case_type_key)
    if case_type is None:
        raise NotFound(f"case type {case_type_key!r} not found")

    from app.application.configuration.publication_history import (  # noqa: PLC0415
        handle as history_handle,  # noqa: PLC0415
    )

    entries = await history_handle(
        PublicationHistoryQuery(
            tenant_id=user.tenant_id,
            case_type_id=case_type.id,
        ),
        case_type_repo=repositories.case_type,
        case_type_version_repo=repositories.case_type_version,
    )
    return PublicationHistoryResponse(
        case_type_id=case_type.id,
        versions=[_to_history(e) for e in entries],
    )


@router.get(
    "/{case_type_key}/versions/{version_id}",
    response_model=CaseTypeVersionDetail,
    summary="Inspect one CaseTypeVersion with full schema and lineage",
)
async def inspect_case_type_version(
    case_type_key: str = Path(min_length=1, max_length=64),
    version_id: UUID = ...,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: AdminCaseTypesHttpDependencyProvider = Depends(get_admin_case_types_dependencies),
) -> CaseTypeVersionDetail:
    """Inspect one version with full schema plus author and effective time.

    The history list carries only the fingerprint; this view carries
    the complete ``schema`` blob with ``published_by`` (author),
    ``published_at`` (effective time) and ``created_at`` (draft time).
    The second-admin approval row and human ``change_summary`` live on
    ``GET /api/v1/admin/publications`` keyed by the same version id.
    Tenant-scoped: cross-tenant ids return 404.
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="case_type"),
        role_repository=repositories.role,
    )
    case_type = await repositories.case_type.get_by_key(user.tenant_id, case_type_key)
    if case_type is None:
        raise NotFound(f"case type {case_type_key!r} not found")
    version = await repositories.case_type_version.get(user.tenant_id, version_id)
    if version is None or version.case_type_id != case_type.id:
        raise NotFound(f"case type version {version_id} not found")
    return _to_detail(case_type_id=case_type.id, version=version)


__all__ = ["router"]
