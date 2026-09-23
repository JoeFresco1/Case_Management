"""Download route — ``GET /api/v1/documents/{document_id}/content``.

Serves authorized document bytes with safe metadata headers
(P03 / FM-022). The endpoint registers onto the package
:mod:`common` ``versions_router``; ``app.main`` includes that router
under ``/api/v1``.

# Rendition semantics

``rendition=original`` serves the pre-redaction bytes (requires the
stricter ``document.read_restricted`` right once a redaction exists or
when the parent is ``restricted`` / ``phi``). ``rendition=redacted``
serves the ACTIVE redaction's bytes and 404s when no redaction exists.
Each rendition appends its own ``DocumentAccessed`` audit row; a
guessed-URL probe without the grant receives 403 with a metadata-free
reason only (Constitution V).

# Failure behavior

A document-store outage surfaces as 503 ``dependency_unavailable``
after rolling back the read transaction — never a stack trace
(AGENTS.md §4.R7).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.application.documents import download_document as download_document_uc
from app.deps import (
    Clock,
    CorrelationContext,
    UserContext,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
)
from app.domain.errors import DependencyUnavailable
from app.domain.ports import DocumentStore
from app.interfaces.http.dependencies import get_case_dependencies

from .common import (
    _content_disposition,
    _safe_content_type,
    get_document_store,
    versions_router,
)


@versions_router.get(
    "/{document_id}/content",
    summary="Download document content",
    response_class=Response,
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.read"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Document store unavailable"},
    },
)
async def download_document(
    document_id: UUID,
    rendition: str = Query(default="original", pattern="^(original|redacted)$"),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    correlation: CorrelationContext = Depends(get_correlation_id),
    document_store: DocumentStore = Depends(get_document_store),
) -> Response:
    """Return authorized document bytes with safe metadata headers.

    ``rendition=original`` serves the pre-redaction bytes (requires
    the stricter ``document.read_restricted`` right once a redaction
    exists or when the parent is ``restricted`` / ``phi``).
    ``rendition=redacted`` serves the ACTIVE redaction's bytes and
    404s when no redaction exists. Each rendition appends its own
    ``DocumentAccessed`` audit row. A guessed-URL probe without the
    grant receives 403 with a metadata-free reason only.
    """
    try:
        result = await download_document_uc.handle(
            download_document_uc.DownloadDocumentCommand(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                document_id=document_id,
                correlation_id=correlation.correlation_id,
                rendition=rendition,  # type: ignore[arg-type]
            ),
            document_repo=dependencies.for_session(db).document,
            document_store=document_store,
            audit=dependencies.for_session(db).audit,
            clock=clock,
        )
    except DependencyUnavailable as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "dependency_unavailable", "message": "document store unavailable"},
        ) from exc

    await db.commit()
    # Why per-rendition content type: the redacted bytes carry their
    # own stored content type; the original keeps the parent's.
    media_type = result.document.content_type
    if result.redaction is not None:
        media_type = result.redaction.content_type
    return Response(
        content=result.content,
        media_type=_safe_content_type(media_type),
        headers={"Content-Disposition": _content_disposition(result.document.file_name)},
    )
