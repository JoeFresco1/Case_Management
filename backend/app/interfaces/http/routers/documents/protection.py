"""Protection routes — redact, lock, restrict, and list redactions (P03-US6/US8 / FM-025).

- ``POST /api/v1/documents/{document_id}/redactions`` — accepts a
  ``multipart/form-data`` upload of the redacted bytes plus text
  fields (``base_version_number``, ``reason``, optional ``region``).
  The use case writes the bytes through the ``DocumentStore`` and
  inserts a ``document_redactions`` row + supersedes the prior ACTIVE
  row in the same transaction. The original ``Document`` +
  ``DocumentVersion`` rows are untouched (P03-US6 — "redactions are
  versioned and preserve the original"). Idempotent via
  ``Idempotency-Key`` (AGENTS.md §10).
- ``POST /api/v1/documents/{document_id}/lock`` — a second lock on an
  already-locked document returns 423 (RFC 4918 §11.3) so a
  misconfigured retry cannot silently re-lock.
- ``POST /api/v1/documents/{document_id}/restrict`` — raises the
  document's sensitivity to ``restricted`` / ``phi`` so all future
  reads move under the stricter ``document.read_restricted`` right. A
  filed (locked) document refuses with 423; a non-restrict target is
  rejected with 422.
- ``GET /api/v1/documents/{document_id}/redactions`` — lists the
  versioned redaction renditions, oldest first, gated by
  ``Action.DOCUMENT_READ``; an unknown or cross-tenant id surfaces as
  404 so a guessed URL never confirms existence.

All register onto the package :mod:`common` ``versions_router``.
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

from app.application.authorization import Action, Resource
from app.application.authorization import authorize as _authorize
from app.application.cases.http_dependencies import CaseHttpDependencyProvider
from app.application.documents import protect_document as protect_document_uc
from app.application.documents import restrict_document as restrict_document_uc
from app.application.documents import upload_document as upload_document_uc
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.errors import DocumentLocked, ValidationFailed
from app.domain.ports import DocumentStore
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.schemas.documents import (
    DocumentRedactionResponse,
    LockDocumentResponse,
    RestrictDocumentRequest,
    RestrictDocumentResponse,
    lock_to_response,
    redaction_to_response,
    restrict_to_response,
)

from .common import get_document_store, versions_router

# ---------------------------------------------------------------------------
# POST /api/v1/documents/{document_id}/redactions (P03-US6 / FM-025)
# ---------------------------------------------------------------------------


@versions_router.post(
    "/{document_id}/redactions",
    response_model=DocumentRedactionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a versioned redaction of a document",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.redact"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "Redacted bytes exceed the configured size limit"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid base_version_number or empty reason"
        },
        status.HTTP_423_LOCKED: {"description": "Document is locked and refuses new redactions"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Document store unavailable"},
    },
)
async def redact_document(
    document_id: UUID,
    content: UploadFile = File(...),
    base_version_number: Annotated[int, Form(ge=1)] = 1,
    reason: Annotated[str, Form(min_length=1, max_length=500)] = "",
    region: Annotated[str | None, Form(min_length=1, max_length=500)] = None,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    document_store: DocumentStore = Depends(get_document_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DocumentRedactionResponse:
    """Create a versioned redaction of an existing document.

    The original document + version rows remain retrievable; only a
    new ``document_redactions`` row is appended (the prior ACTIVE
    redaction, if any, is marked SUPERSEDED in the same transaction).
    A locked document refuses the operation with 423.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    payload = await content.read()
    content_type = (content.content_type or "application/octet-stream").strip()

    # Router-level size pre-check. The case-type policy does not apply
    # to redactions — redactions are post-upload metadata artifacts and
    # the global ceiling protects against unbounded uploads before the
    # storage adapter is called.
    max_size = upload_document_uc.DEFAULT_MAX_SIZE_BYTES
    if max_size and len(payload) > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "code": "validation_failed",
                "message": f"redacted bytes exceed configured max size ({max_size} bytes)",
                "field": "size",
            },
        )

    cmd = protect_document_uc.RedactDocumentCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        document_id=document_id,
        base_version_number=base_version_number,
        content=payload,
        content_type=content_type,
        reason=reason,
        region=region,
    )

    repositories = dependencies.for_session(db)
    document_repo = repositories.document
    audit = repositories.audit
    outbox = repositories.outbox

    try:
        result = await protect_document_uc.redact(
            cmd,
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
    return redaction_to_response(result.redaction)


# ---------------------------------------------------------------------------
# POST /api/v1/documents/{document_id}/lock (P03-US8 / FM-025)
# ---------------------------------------------------------------------------


@versions_router.post(
    "/{document_id}/lock",
    response_model=LockDocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Lock a document so it refuses further mutations",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.lock"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_423_LOCKED: {"description": "Document is already locked"},
    },
)
async def lock_document(
    document_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> LockDocumentResponse:
    """Lock a document so it refuses further versions + redactions."""
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    cmd = protect_document_uc.LockDocumentCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        document_id=document_id,
    )

    repositories = dependencies.for_session(db)
    document_repo = repositories.document
    audit = repositories.audit
    outbox = repositories.outbox

    try:
        result = await protect_document_uc.lock(
            cmd,
            document_repo=document_repo,
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

    await db.commit()
    return lock_to_response(result.document)


# ---------------------------------------------------------------------------
# POST /api/v1/documents/{document_id}/restrict (FM-025 / T5)
# ---------------------------------------------------------------------------


@versions_router.post(
    "/{document_id}/restrict",
    response_model=RestrictDocumentResponse,
    status_code=status.HTTP_200_OK,
    summary="Restrict a document so future reads need the stricter right",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.redact"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid target label"},
        status.HTTP_423_LOCKED: {"description": "Document is filed and refuses change"},
    },
)
async def restrict_document(
    document_id: UUID,
    body: RestrictDocumentRequest,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> RestrictDocumentResponse:
    """Raise a document's sensitivity to ``restricted`` / ``phi``."""
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    cmd = restrict_document_uc.RestrictDocumentCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        document_id=document_id,
        sensitivity=body.sensitivity,
    )

    repositories = dependencies.for_session(db)
    document_repo = repositories.document
    audit = repositories.audit
    outbox = repositories.outbox

    try:
        result = await restrict_document_uc.restrict(
            cmd,
            document_repo=document_repo,
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
    return restrict_to_response(result.document, previous_sensitivity=result.previous_sensitivity)


# ---------------------------------------------------------------------------
# GET /api/v1/documents/{document_id}/redactions (FM-025 rendition list)
# ---------------------------------------------------------------------------


@versions_router.get(
    "/{document_id}/redactions",
    response_model=list[DocumentRedactionResponse],
    summary="List a document's redaction renditions",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.read"},
        status.HTTP_404_NOT_FOUND: {"description": "Document not found in tenant"},
    },
)
async def list_document_redactions(
    document_id: UUID,
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
) -> list[DocumentRedactionResponse]:
    """Return all ``DocumentRedaction`` rows for ``document_id``."""

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
    await _authorize(
        Action.DOCUMENT_READ,
        user,
        Resource(tenant_id=user.tenant_id, kind="document", id=document_id),
    )
    list_fn = getattr(document_repo, "list_redactions", None)
    if list_fn is None:  # pragma: no cover - all adapters implement the port
        return []
    redactions = await list_fn(user.tenant_id, document_id)
    return [redaction_to_response(r) for r in redactions]
