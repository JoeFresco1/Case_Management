"""Viewer annotation routes: overlay CRUD (never touches document bytes).

# INTEGRATION_SEAM: HTTP entry

Annotations are markup pinned to ``(document_id, document_version,
page)``; the annotation path writes only ``viewer_annotations`` rows
plus audit + outbox — the document bytes stay untouched. Authorization
(Action.DOCUMENT_READ / DOCUMENT.annotate) is enforced inside the
application layer; these handlers only translate between HTTP and the
use cases under ``app.application.documents.viewer``.

Endpoints (all under the shared ``/documents`` router):

- ``POST   /api/v1/documents/{document_id}/annotations`` — create one
  overlay mark (idempotency required).
- ``GET    /api/v1/documents/{document_id}/annotations`` — list marks
  oldest-first, optionally filtered by document version.
- ``PATCH  /api/v1/documents/{document_id}/annotations/{annotation_id}``
  — edit geometry/caption only; pinned identity stays immutable
  (idempotency required).
- ``DELETE /api/v1/documents/{document_id}/annotations/{annotation_id}``
  — remove one overlay row, audited (idempotency required).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, status
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
    AnnotationCreateRequest,
    AnnotationResponse,
    AnnotationUpdateRequest,
    annotation_to_response,
)

from .common import _require_idempotency_key, router


@router.post(
    "/{document_id}/annotations",
    response_model=AnnotationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a viewer annotation overlay",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.annotate"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Bad kind/geometry/content"},
    },
)
async def create_annotation(
    document_id: UUID,
    body: AnnotationCreateRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ViewerHttpDependencyProvider = Depends(get_viewer_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> AnnotationResponse:
    """Create one annotation mark over a document version (overlay only)."""
    _require_idempotency_key(idempotency_key)
    repos = dependencies.for_session(db)
    annotation = await viewer_uc.create_annotation(
        viewer_uc.CreateAnnotationCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            document_id=document_id,
            document_version=body.document_version,
            page=body.page,
            kind=body.kind,
            geometry=dict(body.geometry),
            content=body.content,
        ),
        document_repo=repos.document,
        annotation_repo=repos.annotation,
        audit=repos.audit,
        outbox=repos.outbox,
        clock=clock,
    )
    await db.commit()
    return annotation_to_response(annotation)


@router.get(
    "/{document_id}/annotations",
    response_model=list[AnnotationResponse],
    summary="List a document's annotation overlays",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
    },
)
async def list_annotations(
    document_id: UUID,
    document_version: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ViewerHttpDependencyProvider = Depends(get_viewer_dependencies),
) -> list[AnnotationResponse]:
    """Return overlay marks for a document, oldest first.

    Tenant isolation is enforced inside the repositories; an unknown
    document in the actor's tenant surfaces as 404.
    """
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
    marks = await repos.annotation.list_for_document(
        user.tenant_id,
        document_id,
        document_version=document_version,
        limit=limit,
        offset=offset,
    )
    return [annotation_to_response(m) for m in marks]


@router.patch(
    "/{document_id}/annotations/{annotation_id}",
    response_model=AnnotationResponse,
    summary="Edit an annotation overlay (geometry/caption only)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.annotate"},
        status.HTTP_404_NOT_FOUND: {"description": "Annotation not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Bad geometry/content"},
    },
)
async def update_annotation(
    document_id: UUID,
    annotation_id: UUID,
    body: AnnotationUpdateRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ViewerHttpDependencyProvider = Depends(get_viewer_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> AnnotationResponse:
    """Edit an annotation's overlay; pinned identity stays immutable."""
    _require_idempotency_key(idempotency_key)
    repos = dependencies.for_session(db)
    updated = await viewer_uc.update_annotation(
        viewer_uc.UpdateAnnotationCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            document_id=document_id,
            annotation_id=annotation_id,
            geometry=dict(body.geometry) if body.geometry is not None else None,
            content=body.content,
            clear_content=body.clear_content,
        ),
        document_repo=repos.document,
        annotation_repo=repos.annotation,
        audit=repos.audit,
        outbox=repos.outbox,
        clock=clock,
    )
    await db.commit()
    return annotation_to_response(updated)


@router.delete(
    "/{document_id}/annotations/{annotation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an annotation overlay (audited)",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.annotate"},
        status.HTTP_404_NOT_FOUND: {"description": "Annotation not found in tenant"},
    },
)
async def delete_annotation(
    document_id: UUID,
    annotation_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: ViewerHttpDependencyProvider = Depends(get_viewer_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> None:
    """Remove one overlay row; the document bytes are untouched."""
    _require_idempotency_key(idempotency_key)
    repos = dependencies.for_session(db)
    await viewer_uc.delete_annotation(
        viewer_uc.DeleteAnnotationCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            document_id=document_id,
            annotation_id=annotation_id,
        ),
        document_repo=repos.document,
        annotation_repo=repos.annotation,
        audit=repos.audit,
        outbox=repos.outbox,
        clock=clock,
    )
    await db.commit()


__all__ = [
    "create_annotation",
    "delete_annotation",
    "list_annotations",
    "update_annotation",
]
