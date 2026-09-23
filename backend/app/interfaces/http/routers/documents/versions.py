"""Version routes — add a version and list version history (P03-US2 / FR-006).

- ``POST /api/v1/documents/{document_id}/versions`` — write side.
  Adding a version is the same write-side capability as the initial
  upload (it carries the same case-type policy checks) but the URL
  identifies the document directly rather than via its parent case.
  The audit + outbox rows share the transaction with the version
  insert + pointer update. Idempotency: recorded under the
  ``Idempotency-Key`` header (AGENTS.md §10); a retry with the same
  key + same body returns the prior response via
  ``IdempotencyMiddleware``.
- ``GET /api/v1/documents/{document_id}/versions`` — read side.
  Lists the immutable version history of a document in ascending
  version-number order. Used by the case workspace's "History" tab and
  by timeline explainability (FM-023 acceptance — "history explainable
  via timeline").

Both register onto the package :mod:`common` ``versions_router``.
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
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.application.documents import upload_document as upload_document_uc
from app.application.documents import version_document as version_document_uc
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.errors import DocumentLocked, ValidationFailed
from app.domain.ports import DocumentStore
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.schemas.documents import (
    DocumentResponse,
    DocumentVersionResponse,
    document_to_response,
    version_to_response,
)

from .common import get_document_store, versions_router


@versions_router.post(
    "/{document_id}/versions",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a new version to a document",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.upload"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "Upload exceeds the configured size limit"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Disallowed content type"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Document store unavailable"},
    },
)
async def add_document_version(
    document_id: UUID,
    content: UploadFile = File(...),
    content_type_override: Annotated[
        str | None,
        Form(alias="content_type", min_length=1, max_length=255),
    ] = None,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    document_store: DocumentStore = Depends(get_document_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DocumentResponse:
    """Add a new bytes revision to an existing document.

    Accepts ``multipart/form-data`` with the binary ``content`` part
    and an optional ``content_type`` override. Returns the persisted
    ``DocumentResponse`` on success (201) with the new version's
    metadata exposed via ``initial_version``; a rejection returns 422
    with a structured error.
    """
    if not idempotency_key or not idempotency_key.strip():
        # FR-006: every mutation requires Idempotency-Key (AGENTS.md §10).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    payload = await content.read()
    # Prefer the explicit form field when the client provides one;
    # fall back to the multipart part's declared content type.
    content_type_value = (
        content_type_override.strip()
        if content_type_override and content_type_override.strip()
        else (content.content_type or "application/octet-stream").strip()
    )

    # Pre-check the size cap before calling the use case so a too-large
    # upload short-circuits without touching storage. The use case also
    # enforces the cap; this router check is a defense-in-depth.
    max_size = upload_document_uc.DEFAULT_MAX_SIZE_BYTES
    if max_size and len(payload) > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "code": "validation_failed",
                "message": f"document exceeds configured max size ({max_size} bytes)",
                "field": "size",
            },
        )

    cmd = version_document_uc.VersionDocumentCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        document_id=document_id,
        content=payload,
        content_type=content_type_value,
    )

    repositories = dependencies.for_session(db)
    case_repo = repositories.case
    case_type_version_repo = repositories.case_type_version
    document_repo = repositories.document
    audit = repositories.audit
    outbox = repositories.outbox

    try:
        result = await version_document_uc.handle(
            cmd,
            case_repo=case_repo,
            case_type_version_repo=case_type_version_repo,
            document_repo=document_repo,
            document_store=document_store,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    except DocumentLocked as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={
                "code": "document_locked",
                "message": str(exc),
                "field": "document_id",
            },
        ) from exc
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

    await db.commit()
    return document_to_response(result.document, result.version)


# ---------------------------------------------------------------------------
# GET /api/v1/documents/{document_id}/versions (P03-US2 read side)
# ---------------------------------------------------------------------------


@versions_router.get(
    "/{document_id}/versions",
    response_model=list[DocumentVersionResponse],
    summary="List a document's version history",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
    },
)
async def list_document_versions(
    document_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
) -> list[DocumentVersionResponse]:
    """Return all ``DocumentVersion`` rows for ``document_id``.

    Tenant isolation is enforced inside the repository. An empty list
    surfaces as 404 because the document is unknown to the actor's
    tenant.
    """
    document_repo = dependencies.for_session(db).document
    document = await document_repo.get(user.tenant_id, document_id)
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "not_found",
                "message": f"document {document_id} not found",
            },
        )
    versions = await document_repo.list_versions(user.tenant_id, document_id)
    return [version_to_response(v) for v in versions]
