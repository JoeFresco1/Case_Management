"""Upload route — ``POST /api/v1/cases/{case_id}/documents`` (P03-US1 / FM-022 / FR-005).

The endpoint accepts a ``multipart/form-data`` upload with three text
fields (``classification``, ``sensitivity``, ``file_name``) plus the
binary ``content`` part. Authorization is enforced inside the use case
(``Action.DOCUMENT_UPLOAD``); the router only translates between HTTP
and the application layer. Registers onto the package :mod:`common`
case-scoped ``router``.

# Idempotency

Every successful upload is recorded under the ``Idempotency-Key``
header (AGENTS.md §10) so a retry with the same key + same body
returns the prior response. The ``IdempotencyMiddleware`` handles this
for every mutating endpoint under ``/api/v1``; the router still
rejects a missing/blank key with 400 before the use case runs.

# Rejection policy (FR-005 acceptance scenario 2)

An upload that violates the case type's policy (disallowed content
type, oversize, disallowed classification) returns 422 with a
structured ``validation_failed`` error and persists a
``DocumentRejected`` audit event. No metadata row, no version row, no
outbox row.
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
from app.deps import Clock, UserContext, get_clock, get_current_user, get_db
from app.domain.document import DocumentSensitivity
from app.domain.errors import ValidationFailed
from app.domain.ports import DocumentStore
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.schemas.documents import (
    DocumentResponse,
    document_to_response,
)

from .common import _router_max_size, get_document_store, router


@router.post(
    "/{case_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document to a case",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "Missing Idempotency-Key"},
        status.HTTP_403_FORBIDDEN: {"description": "Actor lacks document.upload"},
        status.HTTP_404_NOT_FOUND: {"description": "Case not found in tenant"},
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": "Upload exceeds the configured size limit"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Disallowed content type"},
        status.HTTP_423_LOCKED: {"description": "Document is locked and refuses new versions"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Document store unavailable"},
    },
)
async def upload_document(
    case_id: UUID,
    classification: Annotated[str, Form(min_length=1, max_length=64)],
    sensitivity: Annotated[str, Form(min_length=1, max_length=32)],
    file_name: Annotated[str, Form(min_length=1, max_length=500)],
    content: UploadFile = File(...),
    user: UserContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    dependencies: CaseHttpDependencyProvider = Depends(get_case_dependencies),
    clock: Clock = Depends(get_clock),
    document_store: DocumentStore = Depends(get_document_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> DocumentResponse:
    """Upload a document to a case.

    Accepts ``multipart/form-data`` with the binary ``content`` part
    and three text fields. Returns the persisted ``DocumentResponse``
    on success (201); a rejection returns 422 with a structured error.
    """
    if not idempotency_key or not idempotency_key.strip():
        # FR-005: every mutation requires Idempotency-Key (AGENTS.md §10).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required",
        )

    # Read the content into memory. The DocumentStore writes the bytes
    # to disk in a single atomic step; we deliberately do not stream
    # so the size cap can be enforced before the storage adapter is
    # called. A future slice can move to streaming with a
    # ``UploadFile`` reader + chunked write to ``LocalDocumentStore``.
    payload = await content.read()
    content_type = (content.content_type or "application/octet-stream").strip()

    # Translate the sensitivity text into the enum. Unknown values
    # raise ValidationFailed inside the use case; the router passes
    # the raw string and lets the use case validate.
    try:
        sensitivity_enum = DocumentSensitivity(sensitivity.strip().lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "validation_failed",
                "message": f"unknown sensitivity: {sensitivity!r}",
                "field": "sensitivity",
            },
        ) from exc

    repositories = dependencies.for_session(db)
    audit = repositories.audit
    outbox = repositories.outbox

    # Pre-check the size cap before calling the use case so a too-large
    # upload short-circuits without an audit row. The use case will
    # also enforce the cap; this router check is a defense-in-depth
    # that keeps the audit row count honest.
    max_size = _router_max_size()
    if max_size and len(payload) > max_size:
        cmd_for_audit = upload_document_uc.UploadDocumentCommand(
            tenant_id=user.tenant_id,
            actor_id=user.user_id,
            actor_roles=user.roles,
            case_id=case_id,
            file_name=file_name,
            content_type=content_type,
            classification=classification,
            sensitivity=sensitivity_enum,
            content=payload,
        )
        await upload_document_uc.record_rejection(
            cmd_for_audit,
            audit=audit,
            clock=clock,
            reason="size_exceeds_limit",
            details={"size": len(payload), "max_size": max_size},
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "code": "validation_failed",
                "message": f"document exceeds configured max size ({max_size} bytes)",
                "field": "size",
            },
        )

    cmd = upload_document_uc.UploadDocumentCommand(
        tenant_id=user.tenant_id,
        actor_id=user.user_id,
        actor_roles=user.roles,
        case_id=case_id,
        file_name=file_name,
        content_type=content_type,
        classification=classification,
        sensitivity=sensitivity_enum,
        content=payload,
    )

    repositories = dependencies.for_session(db)
    case_repo = repositories.case
    case_type_version_repo = repositories.case_type_version
    document_repo = repositories.document

    try:
        result = await upload_document_uc.handle(
            cmd,
            case_repo=case_repo,
            case_type_version_repo=case_type_version_repo,
            document_repo=document_repo,
            document_store=document_store,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    except ValidationFailed as exc:
        # FR-005 acceptance scenario 2: the upload is rejected, no
        # metadata or content is written, and a DocumentRejected audit
        # event is recorded.
        await upload_document_uc.record_rejection(
            cmd,
            audit=audit,
            clock=clock,
            reason=exc.field or "validation_failed",
            details={"message": str(exc)},
        )
        await db.commit()
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
