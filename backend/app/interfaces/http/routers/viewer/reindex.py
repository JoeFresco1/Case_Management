"""Viewer re-index route: validated request, rebuild behind the search seam.

# INTEGRATION_SEAM: search index (HTTP entry)

The request + ``DocumentReindexed`` audit + outbox commit in the same
transaction; the search-index adapter drains the outbox event to
rebuild asynchronously. The router only validates the wire input
(blank reason → 422 at the use case) and returns 202 Accepted.

Endpoint (under the shared ``/documents`` router):

- ``POST /api/v1/documents/{document_id}/reindex`` — request a search
  index rebuild for a document (idempotency required).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, Header, status
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
from app.interfaces.http.dependencies import get_viewer_dependencies
from app.interfaces.http.schemas.viewer import (
    ReindexRequestBody,
    ReindexResponse,
)

from .common import _require_idempotency_key, router


@router.post(
    "/{document_id}/reindex",
    response_model=ReindexResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a search-index rebuild for a document",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.reindex"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Blank reason"},
    },
)
async def request_reindex(
    document_id: UUID,
    body: ReindexRequestBody,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ViewerHttpDependencyProvider = Depends(get_viewer_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ReindexResponse:
    """Record a validated re-index request (202: rebuild runs async).

    The request + ``DocumentReindexed`` audit + outbox commit here;
    the search-index adapter drains the outbox event to rebuild.
    """
    _require_idempotency_key(idempotency_key)
    repos = dependencies.for_session(db)
    request = await viewer_uc.request_reindex(
        viewer_uc.ReindexCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            document_id=document_id,
            reason=body.reason,
        ),
        document_repo=repos.document,
        audit=repos.audit,
        outbox=repos.outbox,
        clock=clock,
    )
    await db.commit()
    return ReindexResponse(
        id=request.id,
        document_id=request.document_id,
        reason=request.reason,
        requested_by=request.requested_by,
        requested_at=request.requested_at.isoformat(),
    )


__all__ = ["request_reindex"]
