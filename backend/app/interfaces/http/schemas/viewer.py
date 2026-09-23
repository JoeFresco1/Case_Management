"""Pydantic schemas for the viewer pipeline API (T5 / FM-023 ext).

The wire shapes mirror the :mod:`app.domain.annotations` aggregates.
UUIDs are emitted as strings by the serialization layer; timestamps
are ISO-8601 strings to match the rest of the API. Geometry mappings
pass through verbatim (viewer bounds, never PHI); annotation
``content`` captions are included only when present.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.annotations import (
    Annotation,
    CheckoutLock,
    Rendition,
)


class AnnotationCreateRequest(BaseModel):
    """Body for ``POST /api/v1/documents/{id}/annotations``."""

    document_version: int = Field(ge=1, description="Version the mark is drawn against")
    page: int = Field(ge=1, description="1-based page number")
    kind: str = Field(description="One of highlight, text_box, stamp, freehand")
    geometry: dict[str, Any] = Field(description="Page-relative bounds + style hints")
    content: str | None = Field(default=None, description="Overlay text (required for text_box)")


class AnnotationUpdateRequest(BaseModel):
    """Body for ``PATCH /api/v1/documents/{id}/annotations/{annotation_id}``.

    Only the overlay columns are editable; the pinned
    ``(document_version, page, kind)`` identity is immutable. Set
    ``clear_content`` to remove the caption.
    """

    geometry: dict[str, Any] | None = None
    content: str | None = None
    clear_content: bool = False


class AnnotationResponse(BaseModel):
    """The wire shape of one viewer annotation overlay mark."""

    id: UUID
    tenant_id: UUID
    document_id: UUID
    document_version: int
    page: int
    kind: str
    geometry: dict[str, Any]
    content: str | None
    created_by: UUID
    created_at: str


class CheckoutRequest(BaseModel):
    """Body for ``POST /api/v1/documents/{id}/checkout``."""

    lease_seconds: int = Field(
        default=4 * 60 * 60,
        gt=0,
        le=7 * 24 * 60 * 60,
        description="Checkout lease in seconds (default 4h, max 7d)",
    )


class CheckoutResponse(BaseModel):
    """The wire shape of a checkout lock (acquire, renew, or release)."""

    id: UUID
    document_id: UUID
    holder_user_id: UUID
    acquired_at: str
    expires_at: str
    released_at: str | None
    renewed: bool


class RenditionResponse(BaseModel):
    """The wire shape of one burned viewer rendition."""

    id: UUID
    document_id: UUID
    source_version: int
    kind: str
    sha256: str
    size: int
    content_type: str
    redaction_id: UUID | None
    created_at: str


class RevisionEntryResponse(BaseModel):
    """One row of the unified revision/rendition history view."""

    entry_type: str
    version_number: int
    kind: str
    sha256: str
    size: int
    occurred_at: str
    rendition_id: UUID | None = None
    redaction_id: UUID | None = None


class RevisionHistoryResponse(BaseModel):
    """The unified revision + rendition history for a document."""

    document_id: UUID
    entries: list[RevisionEntryResponse]


class ReindexRequestBody(BaseModel):
    """Body for ``POST /api/v1/documents/{id}/reindex``."""

    reason: str = Field(min_length=1, max_length=500, description="Why the index is rebuilt")


class ReindexResponse(BaseModel):
    """The validated re-index request (rebuild runs behind the seam)."""

    id: UUID
    document_id: UUID
    reason: str
    requested_by: UUID
    requested_at: str


def _iso(dt: datetime) -> str:
    """Render a datetime as an ISO-8601 string (centralized conversion)."""
    return dt.isoformat()


def annotation_to_response(annotation: Annotation) -> AnnotationResponse:
    """Translate a domain annotation into the wire response."""
    return AnnotationResponse(
        id=annotation.id,
        tenant_id=annotation.tenant_id,
        document_id=annotation.document_id,
        document_version=annotation.document_version,
        page=annotation.page,
        kind=annotation.kind.value,
        geometry=dict(annotation.geometry),
        content=annotation.content,
        created_by=annotation.created_by,
        created_at=_iso(annotation.created_at),
    )


def lock_to_response(lock: CheckoutLock, *, renewed: bool) -> CheckoutResponse:
    """Translate a domain checkout lock into the wire response."""
    return CheckoutResponse(
        id=lock.id,
        document_id=lock.document_id,
        holder_user_id=lock.holder_user_id,
        acquired_at=_iso(lock.acquired_at),
        expires_at=_iso(lock.expires_at),
        released_at=_iso(lock.released_at) if lock.released_at else None,
        renewed=renewed,
    )


def rendition_to_response(rendition: Rendition) -> RenditionResponse:
    """Translate a domain rendition into the wire response."""
    return RenditionResponse(
        id=rendition.id,
        document_id=rendition.document_id,
        source_version=rendition.source_version,
        kind=rendition.kind.value,
        sha256=rendition.sha256,
        size=rendition.size,
        content_type=rendition.content_type,
        redaction_id=rendition.redaction_id,
        created_at=_iso(rendition.created_at),
    )


__all__ = [
    "AnnotationCreateRequest",
    "AnnotationResponse",
    "AnnotationUpdateRequest",
    "CheckoutRequest",
    "CheckoutResponse",
    "ReindexRequestBody",
    "ReindexResponse",
    "RenditionResponse",
    "RevisionEntryResponse",
    "RevisionHistoryResponse",
    "annotation_to_response",
    "lock_to_response",
    "rendition_to_response",
]
