"""ORM models for the viewer pipeline (T5 / FM-023 ext).

Split out of :mod:`app.infrastructure.persistence.models` because the
shared file was already past the 1000-line hard cap (AGENTS.md §4.R2).
The tables are otherwise identical in shape to the existing model
families: portable SQLAlchemy generic types only, ``tenant_id`` on
every row, timezone-aware ``DateTime`` timestamps. The module is
imported from ``models/__init__.py`` so ``Base.metadata`` (and the
alembic ``env.py`` target) registers these tables.

Tables:

- ``viewer_annotations`` — one row per
  :class:`app.domain.annotations.Annotation` overlay mark. The overlay
  never mutates document bytes: no FK beyond the owning document, and
  the application layer never writes ``documents`` /
  ``document_versions`` on the annotation path.
- ``document_checkout_locks`` — one row per
  :class:`app.domain.annotations.CheckoutLock`. History is the
  append-only sequence of rows per document; takeover after expiry
  mints a fresh row, release stamps ``released_at``.
- ``document_renditions`` — one row per
  :class:`app.domain.annotations.Rendition` (burned redacted bytes or
  derived descriptors). ``redaction_id`` carries the authorizing mark
  id for the revision-history view; viewer marks are approved inline
  at burn time in v1, so the column has no FK (a persisted multi-step
  mark workflow would add one without changing this contract).

INTEGRATION_SEAM: models are pure SQLAlchemy generic types so the
same DDL works on SQLite (tests) and PostgreSQL / Azure SQL
(production). No JSONB, no GIN, no ``gen_random_uuid()`` (AGENTS.md
§4.R1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ViewerAnnotation(Base):
    """One viewer annotation overlay row (T5 annotation layer).

    Why a separate table (vs columns on ``documents``): a document
    accumulates marks over time and the timeline needs each one as a
    discrete auditable event. The pinned ``document_version`` keeps
    "drawn on v2, viewing v3" explainable without reading bytes.
    """

    __tablename__ = "viewer_annotations"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Why JSON: geometry is viewer bounds + style hints (small, bounded
    # keys); portable generic JSON on SQLite and PostgreSQL/Azure SQL.
    geometry: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_viewer_annotations_tenant_doc", "tenant_id", "document_id"),
        Index(
            "ix_viewer_annotations_tenant_doc_version",
            "tenant_id",
            "document_id",
            "document_version",
        ),
    )


class DocumentCheckoutLock(Base):
    """One persistent checkout row (T5 checkout history).

    The live-lock rule (at most one unreleased, unexpired row per
    document) is enforced by the use case via
    ``CheckoutLock.check_acquire``; the table stores every acquire /
    renew / release so the trail is auditable. ``released_at`` is set
    on checkin; expiry is evaluated by the caller with the Clock port.
    """

    __tablename__ = "document_checkout_locks"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    holder_user_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_checkout_locks_tenant_doc", "tenant_id", "document_id"),
        Index(
            "ix_checkout_locks_tenant_doc_released",
            "tenant_id",
            "document_id",
            "released_at",
        ),
    )


class DocumentRendition(Base):
    """One burned viewer rendition row (T5 revision/rendition history).

    Points at ``source_version`` (the ``DocumentVersion`` it was
    derived from) and carries content addressing so identical burns
    share storage. ``redaction_id`` links a ``REDACTED`` burn back to
    the mark that authorized it; ``ORIGINAL``-kind rows never carry
    one (domain invariant).
    """

    __tablename__ = "document_renditions"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    # Why nullable without FK: v1 authorizes the burn inline (the
    # ``RenditionBurned`` audit row is the approval record); the id
    # links the rendition to its rationale for the history view. A
    # persisted multi-step mark workflow adds the FK later.
    redaction_id: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_document_renditions_tenant_doc", "tenant_id", "document_id"),)


__all__ = [
    "DocumentCheckoutLock",
    "DocumentRendition",
    "ViewerAnnotation",
]
