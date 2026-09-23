"""Imports router (P14-US2, US3, US4 / P04-US3).

Endpoints:

- ``POST /api/v1/imports/dry-run`` — validate a package, produce a
  per-row reconciliation, **never** write any case data.
- ``POST /api/v1/imports/commit``  — re-validate the package and create
  one case per accepted row, plus an attached document when the
  mapping declares one. Rejected rows are recorded in the
  reconciliation; they are quarantined, not silently lost.

# Authorization

Both endpoints require ``ADMIN_TENANT_CONFIG`` (mirrors the outbox /
case-type admin endpoints). The use case enforces the same check.

# Multi-tenant posture

Cases and documents created by a commit inherit the actor's tenant —
the platform is the system of record for migrated cases (P14-Q4). The
mapping's ``case_type_key`` is resolved within the actor's tenant;
cross-tenant case-type references are surfaced as ``NotFound`` so the
existence of cross-tenant configuration is never leaked.

# Synthetic posture (AGENTS.md §18)

The first slice ships with synthetic fixtures only. The router exposes
the pipeline endpoints so the operator UI can preview and commit
synthetic imports during local development; live enterprise data is
gated behind P14's connector authorization (Constitution V.10 /
Pillar P14 §2).
"""

from __future__ import annotations

from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.authorization import Action, Resource, authorize
from app.application.imports import run_import
from app.application.imports.http_dependencies import ImportHttpDependencyProvider
from app.application.imports.run_import import (
    CommitImportCommand,
    ImportRow,
    PreviewImportCommand,
)
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.import_job import (
    DocumentMapping,
    FieldMapping,
    ImportJob,
    ImportReconciliation,
    ImportRowResult,
    MappingVersion,
)
from app.domain.ports import DocumentStore
from app.interfaces.http.dependencies import get_imports_dependencies
from app.interfaces.http.schemas.cases import Page
from app.interfaces.http.schemas.imports import (
    CommitResponse,
    ImportJobListItemResponse,
    ImportJobResponse,
    ImportReconciliationResponse,
    ImportRowResponse,
    ImportRowsPayload,
    MappingVersionRequest,
    PreviewResponse,
)

router = APIRouter(prefix="/imports", tags=["imports"])


def _get_document_store(request: Request) -> DocumentStore:
    """Return the document store selected by the composition root.

    Mirrors ``documents.py``: the store is application-scoped state,
    not a per-session port, so it is resolved here rather than via
    the session-bound provider bundle.
    """
    store = getattr(request.app.state, "document_store", None)
    if store is None:
        raise RuntimeError("document store is not configured")
    return cast(DocumentStore, store)


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


def _mapping_from_request(req: MappingVersionRequest) -> MappingVersion:
    return MappingVersion(
        version=req.version,
        case_type_key=req.case_type_key,
        fields=tuple(FieldMapping(source=f.source, target=f.target) for f in req.fields),
        title_field=req.title_field,
        description_field=req.description_field,
        priority_field=req.priority_field,
        source_id_field=req.source_id_field,
        documents=(
            DocumentMapping(
                file_name_field=req.documents.file_name_field,
                content_field=req.documents.content_field,
                content_type_field=req.documents.content_type_field,
                classification_field=req.documents.classification_field,
                sha256_field=req.documents.sha256_field,
            )
            if req.documents is not None
            else None
        ),
    )


def _rows_from_payload(payload: ImportRowsPayload) -> tuple[ImportRow, ...]:
    return tuple(
        ImportRow(index=row.index, values=dict(row.values), raw=dict(row.values))
        for row in payload.rows
    )


def _mapping_to_dict(mapping: MappingVersion) -> dict[str, Any]:
    return {
        "version": mapping.version,
        "case_type_key": mapping.case_type_key,
        "title_field": mapping.title_field,
        "description_field": mapping.description_field,
        "priority_field": mapping.priority_field,
        "source_id_field": mapping.source_id_field,
        "fields": [{"source": f.source, "target": f.target} for f in mapping.fields],
        "documents": (
            {
                "file_name_field": mapping.documents.file_name_field,
                "content_field": mapping.documents.content_field,
                "content_type_field": mapping.documents.content_type_field,
                "classification_field": mapping.documents.classification_field,
                "sha256_field": mapping.documents.sha256_field,
            }
            if mapping.documents is not None
            else None
        ),
    }


def _result_to_response(result: ImportRowResult) -> ImportRowResponse:
    return ImportRowResponse(
        row_index=result.row_index,
        outcome=result.outcome.value,
        case_id=str(result.case_id) if result.case_id else None,
        source_id=result.source_id,
        error_code=result.error_code,
        error_message=result.error_message,
    )


def _reconciliation_to_response(
    reconciliation: ImportReconciliation | None,
) -> ImportReconciliationResponse | None:
    if reconciliation is None:
        return None
    return ImportReconciliationResponse(
        committed_count=reconciliation.committed_count,
        rejected_count=reconciliation.rejected_count,
        skipped_count=reconciliation.skipped_count,
    )


def _job_to_response(job: ImportJob, mapping_dict: dict[str, Any]) -> ImportJobResponse:
    return ImportJobResponse(
        id=str(job.id),
        tenant_id=str(job.tenant_id),
        source_label=job.source_label,
        source_format=job.source_format,
        source_row_count=job.source_row_count,
        status=job.status,
        mapping=mapping_dict,
        results=[_result_to_response(r) for r in job.results],
        reconciliation=_reconciliation_to_response(job.reconciliation),
        created_at=job.created_at.isoformat(),
        committed_at=job.committed_at.isoformat() if job.committed_at else None,
        failure_reason=job.failure_reason,
        correlation_id=job.correlation_id,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/dry-run",
    response_model=PreviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Validate an import package without writing any case data",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks admin.tenant_config"},
        status.HTTP_404_NOT_FOUND: {"description": "Case type or mapping not found"},
        status.HTTP_409_CONFLICT: {"description": "Idempotency-Key reuse with different payload"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid payload"},
    },
)
async def dry_run_import(
    payload: ImportRowsPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ImportHttpDependencyProvider = Depends(get_imports_dependencies),
) -> PreviewResponse:
    """Run a dry-run preview of an import package.

    The endpoint persists the resulting ``ImportJob`` row so the
    operator can come back to the same preview via the commit endpoint
    (the commit's results must reconcile with the preview's report).

    The ``Idempotency-Key`` header is required (mirrors
    ``capture.py``): the ``IdempotencyMiddleware`` replays an identical
    request verbatim and returns ``409`` when the same key is reused
    with a different payload. The explicit guard keeps the contract
    visible in OpenAPI and fail-closed when the middleware is disabled.
    """
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required for this endpoint",
            },
        )
    repositories = dependencies.for_session(db)
    case_type_repo = repositories.case_type
    case_type_version_repo = repositories.case_type_version
    role_repo = repositories.role

    # Authorization uses the live role assignments so a revoking
    # operator loses access on the next request.
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="import_job"),
        role_repository=role_repo,
    )

    # Resolve mapping: inline mapping (back-compat) or registry reference
    # (FM-026). When mapping_id is provided, the use case resolves it from
    # the registry; when inline mapping is provided, it is used as-is.
    inline_mapping: MappingVersion | None = None
    if payload.mapping is not None:
        inline_mapping = _mapping_from_request(payload.mapping)

    rows = _rows_from_payload(payload)
    correlation_id = str(uuid4())
    cmd = PreviewImportCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        source_label=payload.source_label,
        source_format=payload.source_format,
        mapping=inline_mapping,
        mapping_id=payload.mapping_id,
        rows=rows,
        correlation_id=correlation_id,
    )
    job = await run_import.preview(
        cmd,
        case_type_repo=case_type_repo,
        case_type_version_repo=case_type_version_repo,
        import_mapping_repo=repositories.import_mappings,
        # Read-only: lets the preview report already-imported
        # source-record ids as duplicates. The preview still writes
        # no case, audit, or outbox row.
        case_repo=repositories.case,
    )
    # The job aggregate carries the resolved mapping; use it for the
    # response snapshot (works for both inline and registry-backed paths).
    mapping_dict = _mapping_to_dict(job.mapping)
    await repositories.import_jobs.record(job, mapping_dict)
    await db.commit()
    return PreviewResponse(job=_job_to_response(job, mapping_dict))


@router.post(
    "/commit",
    response_model=CommitResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Commit an import package — create cases (and optional documents)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks admin.tenant_config"},
        status.HTTP_404_NOT_FOUND: {"description": "Case type or mapping not found"},
        status.HTTP_409_CONFLICT: {"description": "Idempotency-Key reuse with different payload"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid payload"},
    },
)
async def commit_import(
    payload: ImportRowsPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    clock: Clock = Depends(get_clock),
    dependencies: ImportHttpDependencyProvider = Depends(get_imports_dependencies),
    document_store: DocumentStore = Depends(_get_document_store),
) -> CommitResponse:
    """Commit an import package.

    The commit re-runs validation and creates one case per accepted row
    (plus an attached document when the mapping declares one). Rejected
    rows are recorded in the reconciliation; they are quarantined, not
    silently dropped.

    The ``Idempotency-Key`` header is required (mirrors
    ``capture.py``): replays return the stored response verbatim and
    key reuse with a different payload returns ``409``.
    """
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key header is required for this endpoint",
            },
        )
    repositories = dependencies.for_session(db)
    case_repo = repositories.case
    case_type_repo = repositories.case_type
    case_type_version_repo = repositories.case_type_version
    document_repo = repositories.document
    audit = repositories.audit
    outbox = repositories.outbox
    role_repo = repositories.role

    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="import_job"),
        role_repository=role_repo,
    )

    # Resolve mapping: inline mapping (back-compat) or registry reference
    # (FM-026). When mapping_id is provided, the use case resolves it from
    # the registry; when inline mapping is provided, it is used as-is.
    inline_mapping: MappingVersion | None = None
    if payload.mapping is not None:
        inline_mapping = _mapping_from_request(payload.mapping)

    rows = _rows_from_payload(payload)
    correlation_id = str(uuid4())
    cmd = CommitImportCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        source_label=payload.source_label,
        source_format=payload.source_format,
        mapping=inline_mapping,
        mapping_id=payload.mapping_id,
        rows=rows,
        correlation_id=correlation_id,
    )
    report = await run_import.commit(
        cmd,
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=case_type_version_repo,
        document_repo=document_repo,
        document_store=document_store,
        audit=audit,
        outbox=outbox,
        clock=clock,
        import_mapping_repo=repositories.import_mappings,
    )
    mapping_dict = _mapping_to_dict(report.job.mapping)
    await repositories.import_jobs.record(report.job, mapping_dict)
    await db.commit()
    return CommitResponse(
        job=_job_to_response(report.job, mapping_dict),
        created_case_ids=[str(c) for c in report.created_case_ids],
    )


# ---------------------------------------------------------------------------
# Read endpoints (FM-027)
# ---------------------------------------------------------------------------


def _job_to_list_item(job: ImportJob) -> ImportJobListItemResponse:
    return ImportJobListItemResponse(
        id=str(job.id),
        source_label=job.source_label,
        source_format=job.source_format,
        source_row_count=job.source_row_count,
        status=job.status,
        created_by=str(job.created_by),
        created_at=job.created_at.isoformat(),
        committed_at=job.committed_at.isoformat() if job.committed_at else None,
    )


@router.get(
    "",
    response_model=Page[ImportJobListItemResponse],
    summary="List import jobs for the current tenant (FM-027)",
)
async def list_import_jobs(
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ImportHttpDependencyProvider = Depends(get_imports_dependencies),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ImportJobListItemResponse]:
    """List import jobs, newest first.

    The operator reviews past previews and commits through this
    endpoint. Each item carries the status so the UI can branch on
    ``previewed`` / ``committed`` / ``failed``.
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="import_job"),
        role_repository=repositories.role,
    )

    jobs = await repositories.import_jobs.list_for_tenant(
        user.tenant_id, limit=limit, offset=offset
    )
    next_cursor: str | None = None
    if len(jobs) == limit:
        next_cursor = f"{offset + limit}:{limit}"
    return Page[ImportJobListItemResponse](
        items=[_job_to_list_item(j) for j in jobs],
        next_cursor=next_cursor,
        limit=limit,
    )


@router.get(
    "/{job_id}",
    response_model=ImportJobResponse,
    summary="Get a single import job by id (FM-027)",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Import job not found"},
    },
)
async def get_import_job(
    job_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ImportHttpDependencyProvider = Depends(get_imports_dependencies),
) -> ImportJobResponse:
    """Return the full preview/commit report for one import job.

    The response includes the per-row reconciliation so the operator
    can inspect which rows were accepted and which were quarantined
    with a stable error code.
    """
    repositories = dependencies.for_session(db)
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        user,
        Resource(tenant_id=user.tenant_id, kind="import_job"),
        role_repository=repositories.role,
    )

    job = await repositories.import_jobs.get(user.tenant_id, job_id)
    if job is None:
        # why dict detail: FastAPI renders ``detail`` verbatim, so a
        # plain string would break the ``ErrorResponse{code,message}``
        # contract (AGENTS.md §4.R7). The dict keeps 404s structured
        # and never leaks whether a foreign tenant owns the id.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "not_found",
                "message": f"import job {job_id} not found",
            },
        )
    mapping_dict = _mapping_to_dict(job.mapping)
    return _job_to_response(job, mapping_dict)


__all__ = ["router"]
