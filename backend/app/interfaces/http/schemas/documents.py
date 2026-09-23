"""Pydantic schemas for the documents API (P03-US1 / FR-005).

The response shape mirrors the ``Document`` aggregate's wire-safe
fields. UUIDs are emitted as strings (the schema's serialization
layer handles that). Timestamps are ISO 8601 strings to match the
rest of the API.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.domain.document import (
    Document,
    DocumentRedaction,
    DocumentSensitivity,
    DocumentVersion,
)


class DocumentResponse(BaseModel):
    """The wire shape returned by ``POST /api/v1/cases/{id}/documents``.

    Carries the metadata of the persisted document plus the initial
    version row so the client can render "v1 of <file>" without a
    second round trip. Subsequent versions land via the add-version
    endpoint (FM-023 / P03-US2 — deferred).
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    file_name: str
    content_type: str
    classification: str
    sensitivity: DocumentSensitivity
    sha256: str
    size: int
    current_version: int
    storage_ref: str
    uploaded_by: UUID
    created_at: str
    updated_at: str
    initial_version: DocumentVersionResponse


class DocumentVersionResponse(BaseModel):
    """The wire shape of a single ``DocumentVersion``."""

    id: UUID
    document_id: UUID
    version_number: int
    sha256: str
    size: int
    storage_ref: str
    content_type: str
    uploaded_by: UUID
    uploaded_at: str


class DocumentRedactionResponse(BaseModel):
    """The wire shape of a single ``DocumentRedaction`` (P03-US6 / FM-025)."""

    id: UUID
    document_id: UUID
    base_version_number: int
    reason: str
    sha256: str
    size: int
    storage_ref: str
    content_type: str
    region: str | None
    status: str
    created_by: UUID
    created_at: str
    superseded_at: str | None
    superseded_by: UUID | None


class LockDocumentResponse(BaseModel):
    """The wire shape returned by ``POST /api/v1/documents/{id}/lock``.

    Carries the locked metadata so the caller can render "locked at/by"
    without a second round trip.
    """

    id: UUID
    locked: bool
    locked_at: str | None
    locked_by: UUID | None


class RestrictDocumentRequest(BaseModel):
    """The wire shape for ``POST /api/v1/documents/{id}/restrict``.

    Only ``restricted`` / ``phi`` are accepted (raising the label moves
    future reads under the stricter right). Lowering the label is a
    separate declassification flow and is rejected with 422.
    """

    sensitivity: DocumentSensitivity


class RestrictDocumentResponse(BaseModel):
    """The wire shape returned by the restrict endpoint (FM-025)."""

    id: UUID
    sensitivity: DocumentSensitivity
    previous_sensitivity: DocumentSensitivity
    updated_at: str


def _iso(dt: datetime) -> str:
    """Render a datetime as an ISO-8601 string.

    The domain aggregates already produce UTC-aware timestamps; this
    helper exists so the conversion is centralized and the test
    surface stays small.
    """
    return dt.isoformat()


def document_to_response(document: Document, version: DocumentVersion) -> DocumentResponse:
    """Translate the domain aggregates into the wire response.

    The ``initial_version`` field carries the first ``DocumentVersion``
    row that was appended to the database in the same transaction as
    the metadata.
    """
    return DocumentResponse(
        id=document.id,
        tenant_id=document.tenant_id,
        case_id=document.case_id,
        file_name=document.file_name,
        content_type=document.content_type,
        classification=document.classification,
        sensitivity=document.sensitivity,
        sha256=document.sha256,
        size=document.size,
        current_version=document.current_version,
        storage_ref=document.storage_ref,
        uploaded_by=document.uploaded_by,
        created_at=_iso(document.created_at),
        updated_at=_iso(document.updated_at),
        initial_version=DocumentVersionResponse(
            id=version.id,
            document_id=version.document_id,
            version_number=version.version_number,
            sha256=version.sha256,
            size=version.size,
            storage_ref=version.storage_ref,
            content_type=version.content_type,
            uploaded_by=version.uploaded_by,
            uploaded_at=_iso(version.uploaded_at),
        ),
    )


def version_to_response(version: DocumentVersion) -> DocumentVersionResponse:
    """Translate a single ``DocumentVersion`` into the wire response.

    Used by ``GET /api/v1/documents/{id}/versions`` so the client can
    render the version history without seeing the metadata aggregate.
    """
    return DocumentVersionResponse(
        id=version.id,
        document_id=version.document_id,
        version_number=version.version_number,
        sha256=version.sha256,
        size=version.size,
        storage_ref=version.storage_ref,
        content_type=version.content_type,
        uploaded_by=version.uploaded_by,
        uploaded_at=_iso(version.uploaded_at),
    )


def redaction_to_response(redaction: DocumentRedaction) -> DocumentRedactionResponse:
    """Translate a ``DocumentRedaction`` aggregate into the wire response."""
    return DocumentRedactionResponse(
        id=redaction.id,
        document_id=redaction.document_id,
        base_version_number=redaction.base_version_number,
        reason=redaction.reason,
        sha256=redaction.sha256,
        size=redaction.size,
        storage_ref=redaction.storage_ref,
        content_type=redaction.content_type,
        region=redaction.region,
        status=redaction.status,
        created_by=redaction.created_by,
        created_at=_iso(redaction.created_at),
        superseded_at=(_iso(redaction.superseded_at) if redaction.superseded_at else None),
        superseded_by=redaction.superseded_by,
    )


def lock_to_response(document: Document) -> LockDocumentResponse:
    """Translate a locked ``Document`` aggregate into the wire response."""
    return LockDocumentResponse(
        id=document.id,
        locked=document.locked,
        locked_at=_iso(document.locked_at) if document.locked_at else None,
        locked_by=document.locked_by,
    )


def restrict_to_response(
    document: Document, *, previous_sensitivity: DocumentSensitivity
) -> RestrictDocumentResponse:
    """Translate a restricted ``Document`` aggregate into the wire response."""
    return RestrictDocumentResponse(
        id=document.id,
        sensitivity=document.sensitivity,
        previous_sensitivity=previous_sensitivity,
        updated_at=_iso(document.updated_at),
    )


__all__ = [
    "DocumentRedactionResponse",
    "DocumentResponse",
    "DocumentVersionResponse",
    "LockDocumentResponse",
    "RestrictDocumentRequest",
    "RestrictDocumentResponse",
    "document_to_response",
    "lock_to_response",
    "redaction_to_response",
    "restrict_to_response",
    "version_to_response",
]


# Pydantic v2: forward references for the nested model. ``model_rebuild``
# resolves the ``initial_version`` field declared as a string above.
DocumentResponse.model_rebuild()
