"""In-case document search router (P03-US3 / P06-US8 / FM-033).

Exposes ``GET /api/v1/cases/{id}/documents/search``. The endpoint is
deliberately scoped to a single case: searching across multiple
cases is a future enhancement (FM-034 / semantic search) and the
in-case surface is the documented first slice (P03-US3 — "find
documents within a case by metadata").

The router only translates between the wire shape and the
application shape; no business logic lives here. The use case
(``app.application.search.document_search.handle``) authorizes the
actor against ``Action.DOCUMENT_READ``, validates the filter shape
against the case-type's ``document_classifications`` allowlist, and
delegates the actual execution to
``SqlDocumentSearchProvider``.

# INTEGRATION_SEAM: search engine

The router wires the SQL adapter; an Azure Cognitive Search /
OpenSearch adapter replaces the implementation behind the same
``DocumentSearchProvider`` Protocol without touching this file.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.search import document_search as document_search_uc
from app.application.search.http_dependencies import (
    DocumentSearchHttpDependencyProvider,
)
from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import NotFound, ValidationFailed
from app.domain.ports import DocumentSearchHit
from app.interfaces.http.dependencies import get_document_search_dependencies
from app.interfaces.http.schemas.search import (
    DocumentSearchHit as DocumentSearchHitWire,
)
from app.interfaces.http.schemas.search import DocumentSearchResponse

router = APIRouter(prefix="/cases", tags=["search"])


def _hit_to_wire(hit: DocumentSearchHit) -> DocumentSearchHitWire:
    """Translate the application ``DocumentSearchHit`` into the wire shape.

    The snippet's value is passed through unchanged; per-document
    redaction has already happened in the SQL adapter (P11-US6 /
    P03-US3). The wire shape is a Pydantic model so the OpenAPI
    schema is honest about the mix of strings and ``Redacted``
    markers.
    """
    snippet: dict[str, Any] | str = (
        hit.snippet if isinstance(hit.snippet, dict) else str(hit.snippet or "")
    )
    return DocumentSearchHitWire(
        id=hit.document_id,
        case_id=hit.case_id,
        file_name=hit.file_name,
        classification=hit.classification,
        sensitivity=hit.sensitivity,
        current_version=hit.current_version,
        content_type=hit.content_type,
        sha256=hit.sha256,
        uploaded_by=hit.uploaded_by,
        uploaded_at=hit.uploaded_at.isoformat(),
        snippet=snippet,
    )


@router.get(
    "/{case_id}/documents/search",
    response_model=DocumentSearchResponse,
    summary="Search documents within a case (metadata + portable LIKE)",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Validation failed (unknown classification, non-searchable field, etc.)"
        },
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.read"},
        status.HTTP_404_NOT_FOUND: {"description": "Case not found in tenant"},
    },
)
async def get_document_search(
    case_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: DocumentSearchHttpDependencyProvider = Depends(get_document_search_dependencies),
    query: Annotated[str | None, Query(max_length=500)] = None,
    classification: Annotated[str | None, Query(max_length=64)] = None,
    uploaded_by: Annotated[UUID | None, Query()] = None,
    uploaded_since: Annotated[
        datetime | None,
        Query(description="Inclusive lower bound on uploaded_at (ISO-8601)"),
    ] = None,
    uploaded_until: Annotated[
        datetime | None,
        Query(description="Inclusive upper bound on uploaded_at (ISO-8601)"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentSearchResponse:
    """Search documents attached to a case (P03-US3 / FM-033).

    The query is a portable substring over ``file_name`` (case-
    insensitive). Metadata filters restrict by classification,
    uploader, and upload date. The result set is filtered by the
    case-type's classification allowlist and the actor's
    sensitive-read grant so the response never widens the actor's
    visibility (P11-US6 / FM-029).
    """
    repositories = dependencies.for_session(db)
    provider = repositories.document_search
    case_repo = repositories.case
    case_type_version_repo = repositories.case_type_version
    try:
        result = await document_search_uc.handle(
            document_search_uc.DocumentSearchQuery(
                tenant_id=user.tenant_id,
                actor_id=user.user_id,
                actor_roles=user.roles,
                case_id=case_id,
                text_query=query,
                classification=classification,
                uploaded_by=uploaded_by,
                uploaded_since=uploaded_since,
                uploaded_until=uploaded_until,
                limit=limit,
                offset=offset,
            ),
            case_repo=case_repo,
            case_type_version_repo=case_type_version_repo,
            document_search_provider=provider,
        )
    except ValidationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "validation_failed",
                "message": str(exc),
                "field": exc.field,
            },
        ) from exc
    except NotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": str(exc)},
        ) from exc

    return DocumentSearchResponse(
        items=[_hit_to_wire(h) for h in result.items],
        next_cursor=result.next_cursor,
        limit=result.limit,
        total_considered=result.total_considered,
    )


__all__ = ["router"]
