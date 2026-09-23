"""Viewer revision + rendition routes: unified history and rendition burn.

# INTEGRATION_SEAM: HTTP entry (+ DocumentStore seam for burned bytes)

Versions are the immutable originals; renditions are viewer burns
derived from a source version. Burning stores *new* bytes through the
shared ``DocumentStore`` seam (``get_document_store`` resolves the
adapter off ``app.state``) — the original ``DocumentVersion`` rows stay
untouched, so both sides stay retrievable and audited. Re-index and
annotation paths live in sibling modules.

Endpoints (all under the shared ``/documents`` router):

- ``GET  /api/v1/documents/{document_id}/revisions`` — filed versions +
  burned renditions as one oldest-first timeline.
- ``GET  /api/v1/documents/{document_id}/renditions`` — burned
  rendition rows oldest-first (bytes stay in the store).
- ``POST /api/v1/documents/{document_id}/renditions`` — burn approved
  redaction bytes as a new ``REDACTED`` rendition (idempotency
  required; original kept).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import (
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.documents import viewer as viewer_uc
from app.application.documents.http_dependencies import ViewerHttpDependencyProvider
from app.deps import (
    Clock,
    UserContext,
    get_clock,
    get_current_user,
    get_db,
)
from app.domain.ports import DocumentStore
from app.interfaces.http.dependencies import get_viewer_dependencies
from app.interfaces.http.routers.documents import get_document_store
from app.interfaces.http.schemas.viewer import (
    RenditionResponse,
    RevisionEntryResponse,
    RevisionHistoryResponse,
    rendition_to_response,
)

from .common import _require_idempotency_key, router


@router.get(
    "/{document_id}/revisions",
    response_model=RevisionHistoryResponse,
    summary="Unified revision/rendition history for a document",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
    },
)
async def get_revisions(
    document_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ViewerHttpDependencyProvider = Depends(get_viewer_dependencies),
) -> RevisionHistoryResponse:
    """Return filed versions + burned renditions oldest-first.

    Versions are the immutable originals; renditions are viewer burns
    derived from a source version. The viewer renders them as one
    timeline ("v2 filed, redacted burn of v2, v3 filed").
    """
    repos = dependencies.for_session(db)
    history = await viewer_uc.revision_history(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        document_id=document_id,
        document_repo=repos.document,
        rendition_repo=repos.rendition,
    )
    return RevisionHistoryResponse(
        document_id=history.document_id,
        entries=[
            RevisionEntryResponse(
                entry_type=e.entry_type,
                version_number=e.version_number,
                kind=e.kind,
                sha256=e.sha256,
                size=e.size,
                occurred_at=e.occurred_at.isoformat(),
                rendition_id=e.rendition_id,
                redaction_id=e.redaction_id,
            )
            for e in history.entries()
        ],
    )


@router.get(
    "/{document_id}/renditions",
    response_model=list[RenditionResponse],
    summary="List a document's burned renditions",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
    },
)
async def list_renditions(
    document_id: UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ViewerHttpDependencyProvider = Depends(get_viewer_dependencies),
) -> list[RenditionResponse]:
    """Return burned rendition rows oldest-first (bytes stay in the store)."""
    from app.application.authorization import (  # noqa: PLC0415
        Action,
        Resource,
        authorize,
    )

    await authorize(
        Action.DOCUMENT_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="document", id=document_id),
    )
    repos = dependencies.for_session(db)
    document = await repos.document.get(user.tenant_id, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "not_found",
                "message": f"document {document_id} not found",
            },
        )
    renditions = await repos.rendition.list_for_document(
        user.tenant_id, document_id, limit=limit, offset=offset
    )
    return [rendition_to_response(r) for r in renditions]


@router.post(
    "/{document_id}/renditions",
    response_model=RenditionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Burn approved redaction bytes as a new rendition",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.redact"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Bad source version"},
    },
)
async def burn_rendition(
    document_id: UUID,
    content: UploadFile = File(...),
    source_version: Annotated[int, Form(ge=1)] = 1,
    reason: Annotated[str, Form(min_length=1, max_length=500)] = "",
    region: Annotated[str | None, Form(min_length=1, max_length=500)] = None,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ViewerHttpDependencyProvider = Depends(get_viewer_dependencies),
    clock: Clock = Depends(get_clock),
    document_store: DocumentStore = Depends(get_document_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RenditionResponse:
    """Store burned bytes as a new ``REDACTED`` rendition (original kept).

    The source ``DocumentVersion`` rows are never modified, so the
    original stays retrievable; both sides are audited.
    """
    _require_idempotency_key(idempotency_key)
    payload = await content.read()
    content_type = (content.content_type or "application/octet-stream").strip()
    repos = dependencies.for_session(db)
    rendition = await viewer_uc.burn_rendition(
        viewer_uc.BurnRenditionCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            document_id=document_id,
            source_version=source_version,
            content=payload,
            content_type=content_type,
            reason=reason,
            region=region or "",
        ),
        document_repo=repos.document,
        rendition_repo=repos.rendition,
        document_store=document_store,
        audit=repos.audit,
        outbox=repos.outbox,
        clock=clock,
    )
    await db.commit()
    return rendition_to_response(rendition)


__all__ = ["burn_rendition", "get_revisions", "list_renditions"]
