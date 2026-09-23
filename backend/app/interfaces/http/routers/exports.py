"""Evidence bundle router: ``POST /api/v1/exports/bundle`` (P14-US8 / FM-060).

The endpoint produces a portable, ZIP-encoded archive of one case:
the case aggregate (with restricted fields redacted per the
case-type schema), every document attached to the case, and the
full per-case audit timeline (with the chain anchors a downstream
auditor needs to verify integrity). The archive is its own audit
event so the action is visible on the chain (P09-US5 — "the export
itself is an audit event").

# Authorization (Constitution V / AGENTS.md §4.R12)

The handler authorizes via the use case (``Action.CASE_READ`` and
``Action.DOCUMENT_READ``). A forbidden actor receives 403; a
missing or cross-tenant case receives 404. The two are
intentionally distinct so a forbidden actor cannot probe for case
existence (Constitution V).

# Streaming response

The archive is built in memory and returned as a single
``StreamingResponse`` so the client can pipe the body straight to
disk. The use case's :class:`EvidenceBundle` carries both a
pre-built ``archive`` blob (for tests + CLI) and an ``entries``
list (for routers that want finer-grained streaming); the router
uses the pre-built blob because the bundle is bounded by the
case's document count + audit row count, both of which are
tenant-scoped and reasonable.

# Synthetic posture (AGENTS.md §18)

The endpoint runs in the synthetic environment by default. The
bundle's manifest includes the ``exported_at`` / ``exported_by``
metadata so the operator console can show "last exported" on the
case workspace without an extra read.
"""

from __future__ import annotations

import io
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.exports import evidence_bundle as evidence_bundle_uc
from app.application.exports.http_dependencies import (
    EvidenceBundleHttpDependencyProvider,
)
from app.deps import (
    Clock,
    CorrelationContext,
    SystemClock,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.errors import NotFound, ValidationFailed
from app.domain.ports import DocumentStore
from app.interfaces.http.dependencies import get_exports_dependencies
from app.interfaces.http.schemas.exports import EvidenceBundleRequest

router = APIRouter(prefix="/exports", tags=["exports"])


def get_document_store(request: Request) -> DocumentStore:
    """Return the document store selected by the composition root.

    Mirrors the documents router's helper: the process-wide store is
    installed on ``request.app.state.document_store`` by the
    composition root (local filesystem today, Azure Blob in
    production) without importing the infrastructure adapter here
    (AGENTS.md §12).
    """
    store = getattr(request.app.state, "document_store", None)
    if store is None:
        raise RuntimeError("document store is not configured")
    return cast(DocumentStore, store)


@router.post(
    "/bundle",
    response_class=StreamingResponse,
    summary="Export a portable evidence bundle (case + documents + audit)",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Actor lacks case.read or document.read on the case"
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Case not found in tenant (also surfaces cross-tenant probes)"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Missing or empty reason"},
    },
    status_code=status.HTTP_200_OK,
)
async def post_evidence_bundle(
    payload: Annotated[
        EvidenceBundleRequest,
        "Body describing the case to export, the reason, and the redaction posture",
    ],
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: EvidenceBundleHttpDependencyProvider = Depends(get_exports_dependencies),
    clock: Clock = Depends(get_clock),
    correlation: CorrelationContext = Depends(get_correlation_id),
    document_store: DocumentStore = Depends(get_document_store),
) -> StreamingResponse:
    """Build the evidence bundle for ``payload.case_id`` and stream it back.

    The handler:

    1. Translates the request into the application-layer
       :class:`BuildEvidenceBundleCommand`.
    2. Calls :func:`evidence_bundle.handle` with the in-memory and
       SQL adapters wired from the request's session.
    3. Streams the pre-built ZIP archive as ``application/zip``
       with a deterministic filename so the client can save it
       without renaming.

    The export appends an ``AuditExported`` row with
    ``bundle=True`` so the action is visible on the chain. The
    handler commits the audit row in the same transaction as the
    read so a failure rolls back both.
    """
    repositories = dependencies.for_session(db)
    case_repo = repositories.case
    case_type_version_repo = repositories.case_type_version
    document_repo = repositories.document
    audit = repositories.audit

    cmd = evidence_bundle_uc.BuildEvidenceBundleCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=payload.case_id,
        reason=payload.reason,
        can_see_sensitive=payload.can_see_sensitive,
        correlation_id=correlation.correlation_id,
    )

    try:
        bundle = await evidence_bundle_uc.handle(
            cmd,
            case_repo=case_repo,
            case_type_version_repo=case_type_version_repo,
            document_repo=document_repo,
            document_store=document_store,
            audit=audit,
            dedupe=repositories.dedupe,
            clock=clock if not isinstance(clock, SystemClock) else None,
        )
    except ValidationFailed as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "validation_failed",
                "message": str(exc),
                "field": exc.field,
            },
        ) from exc
    except NotFound as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "not_found",
                "message": str(exc),
            },
        ) from exc

    await db.commit()

    file_name = _bundle_file_name(case_id=payload.case_id, manifest_hash=bundle.manifest_hash)
    headers = {
        "Content-Disposition": f'attachment; filename="{file_name}"',
        "X-Bundle-Manifest-Sha256": bundle.manifest_hash,
        "X-Document-Count": str(bundle.document_count),
        "X-Audit-Row-Count": str(bundle.audit_row_count),
    }
    return StreamingResponse(
        io.BytesIO(bundle.archive),
        media_type="application/zip",
        headers=headers,
    )


def _bundle_file_name(*, case_id: UUID, manifest_hash: str) -> str:
    """Build a deterministic, filesystem-safe filename for the bundle.

    The filename embeds the case id and a short prefix of the
    manifest hash so multiple exports of the same case do not
    collide on disk. The full hash is in the response header so a
    downstream tool can verify the body without parsing the
    filename.
    """
    return f"case-{case_id}-evidence-bundle-{manifest_hash[:12]}.zip"


__all__ = ["router"]
