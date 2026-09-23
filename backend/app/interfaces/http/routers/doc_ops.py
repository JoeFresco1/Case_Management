"""Document-operations router: x-refs, separation plans, packages, dedup, CSV (T5).

# INTEGRATION_SEAM: HTTP entry

All endpoints authorize inside the use case (``Action.DOCUMENT_READ``
and friends); the router only translates between HTTP and the
application layer. X-ref / package / separation-plan / bundle stores
are process-local singletons on ``app.state`` (synthetic tenants; a
SQL adapter replaces the Protocols without touching this file).

Idempotency: every successful mutation is recorded under the
``Idempotency-Key`` header (AGENTS.md §10). The
``IdempotencyMiddleware`` replays prior responses for retries.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.application.documents import doc_ops as doc_ops_uc
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.schemas.doc_ops import (
    BuildPackageRequest,
    CreateXrefRequest,
    DedupResponse,
    ExportCsvRequest,
    PackageManifestResponse,
    PlanJoinMergeRequest,
    PlanSplitRequest,
    SeparationPlanResponse,
    XrefResponse,
    xref_to_response,
)

router = APIRouter(tags=["document-operations"])


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def get_xref_store(request: Request) -> doc_ops_uc.XrefStore:
    """Return the x-ref store selected by the composition root."""
    store = getattr(request.app.state, "xref_store", None)
    if store is None:
        raise RuntimeError("xref store is not configured")
    return cast(doc_ops_uc.XrefStore, store)


def _require_idempotency_key(idempotency_key: str | None) -> None:
    """Reject mutations without an ``Idempotency-Key`` header (FR-005)."""
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )


# ---------------------------------------------------------------------------
# POST /api/v1/documents/{id}/xrefs + GET .../xrefs
# ---------------------------------------------------------------------------


@router.post(
    "/documents/{document_id}/xrefs",
    response_model=XrefResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a cross-document reference",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.read"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Self-reference or bad label"},
    },
)
async def create_xref(
    document_id: UUID,
    body: CreateXrefRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    xref_store: doc_ops_uc.XrefStore = Depends(get_xref_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> XrefResponse:
    """Create an x-ref from ``document_id`` to ``body.target_document_id``."""
    _require_idempotency_key(idempotency_key)
    repositories = dependencies.for_session(db)
    xref = await doc_ops_uc.create_xref(
        doc_ops_uc.CreateXrefCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            source_document_id=document_id,
            target_document_id=body.target_document_id,
            label=body.label,
            sort_order=body.sort_order,
        ),
        document_repo=repositories.document,
        xref_store=xref_store,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return xref_to_response(xref)


@router.get(
    "/documents/{document_id}/xrefs",
    response_model=list[XrefResponse],
    summary="List cross-document references for a document",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.read"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
    },
)
async def list_xrefs(
    document_id: UUID,
    sort_by: str = Query(default="sort_order", pattern="^(sort_order|created_at|label)$"),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    xref_store: doc_ops_uc.XrefStore = Depends(get_xref_store),
) -> list[XrefResponse]:
    """Return x-refs citing ``document_id`` in deterministic display order."""
    repositories = dependencies.for_session(db)
    xrefs = await doc_ops_uc.list_xrefs(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        source_document_id=document_id,
        sort_by=sort_by,
        document_repo=repositories.document,
        xref_store=xref_store,
    )
    return [xref_to_response(x) for x in xrefs]


# ---------------------------------------------------------------------------
# POST /api/v1/documents/{id}/separation/split + join-merge (plan-only)
# ---------------------------------------------------------------------------


@router.post(
    "/documents/{document_id}/separation/split",
    response_model=SeparationPlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Validate a separation split plan (no byte writes)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.read"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Non-partitioning geometry"},
        status.HTTP_423_LOCKED: {"description": "Source document is filed"},
    },
)
async def plan_split(
    document_id: UUID,
    body: PlanSplitRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    correlation: CorrelationContext = Depends(get_correlation_id),
) -> SeparationPlanResponse:
    """Validate split geometry and record the page-mapping audit trail."""
    _require_idempotency_key(idempotency_key)
    _ = correlation
    repositories = dependencies.for_session(db)
    plan = await doc_ops_uc.plan_separation_split(
        doc_ops_uc.PlanSplitCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            case_id=body.case_id,
            source_document_id=document_id,
            page_count=body.page_count,
            ranges=tuple((r.start_page, r.end_page) for r in body.ranges),
            reason=body.reason,
        ),
        document_repo=repositories.document,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return SeparationPlanResponse(**plan.record.describe())


@router.post(
    "/documents/separation/join-merge",
    response_model=SeparationPlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Validate a separation join/merge plan (no byte writes)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.read"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid sources or operation"},
        status.HTTP_423_LOCKED: {"description": "A source document is filed"},
    },
)
async def plan_join_merge(
    body: PlanJoinMergeRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SeparationPlanResponse:
    """Validate join/merge sources and record the audit trail."""
    _require_idempotency_key(idempotency_key)
    repositories = dependencies.for_session(db)
    plan = await doc_ops_uc.plan_separation_join_merge(
        doc_ops_uc.PlanJoinMergeCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            case_id=body.case_id,
            operation=body.operation,
            source_document_ids=tuple(body.source_document_ids),
            page_counts=tuple(body.page_counts),
            reason=body.reason,
        ),
        document_repo=repositories.document,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return SeparationPlanResponse(**plan.record.describe())


# ---------------------------------------------------------------------------
# POST /api/v1/documents/packages (manifest-bearing export envelope)
# ---------------------------------------------------------------------------


@router.post(
    "/documents/packages",
    response_model=PackageManifestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Build a multi-document export package with manifest",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.read"},
        status.HTTP_404_NOT_FOUND: {"description": "Member document not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Empty or oversized member list"},
    },
)
async def build_package(
    body: BuildPackageRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PackageManifestResponse:
    """Assemble a manifest-bearing package over tenant-scoped documents."""
    _require_idempotency_key(idempotency_key)
    repositories = dependencies.for_session(db)
    package = await doc_ops_uc.build_package(
        doc_ops_uc.BuildPackageCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            name=body.name,
            member_document_ids=tuple(body.member_document_ids),
        ),
        document_repo=repositories.document,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return PackageManifestResponse(manifest=package.manifest())


# ---------------------------------------------------------------------------
# GET /api/v1/cases/{id}/documents/duplicates + POST .../csv-export
# ---------------------------------------------------------------------------


@router.get(
    "/cases/{case_id}/documents/duplicates",
    response_model=DedupResponse,
    summary="Detect duplicate uploads by content hash",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.read"},
    },
)
async def find_duplicates(
    case_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
) -> DedupResponse:
    """Cluster one case's documents by SHA-256 (exact-digest groups)."""
    repositories = dependencies.for_session(db)
    groups = await doc_ops_uc.find_duplicates(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=case_id,
        document_repo=repositories.document,
    )
    return DedupResponse(groups=[list(group) for group in groups])


@router.post(
    "/cases/{case_id}/documents/csv-export",
    response_class=PlainTextResponse,
    summary="Export an authorized CSV projection of a case's documents",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.read"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Unknown column"},
    },
)
async def export_csv(
    case_id: UUID,
    body: ExportCsvRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> PlainTextResponse:
    """Render an allowlisted CSV export (restricted rows drop w/o grant)."""
    _require_idempotency_key(idempotency_key)
    repositories = dependencies.for_session(db)
    text = await doc_ops_uc.export_csv(
        doc_ops_uc.ExportCsvCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            case_id=case_id,
            columns=tuple(body.columns),
        ),
        document_repo=repositories.document,
        audit=repositories.audit,
        outbox=repositories.outbox,
        clock=clock,
    )
    await db.commit()
    return PlainTextResponse(content=text, media_type="text/csv")


__all__ = ["get_xref_store", "router"]
