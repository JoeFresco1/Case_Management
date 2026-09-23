"""Document ORM models: documents, immutable versions, and redactions.

Bytes live in the ``DocumentStore`` port; these rows carry metadata only.
Split verbatim from the pre-split ``persistence/models.py`` (AGENTS.md §4.R2).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

from .base import TimestampMixin

# ---------------------------------------------------------------------------
# Document + DocumentVersion (P03-US1, US2 / FM-022 / AGENTS.md §16.1).
#
# Bytes live in the content-addressable ``DocumentStore`` port; the
# relational store only carries metadata + version rows. ``sha256`` is
# the canonical content key and is recorded on both rows so an audit
# replay can reconstruct "which bytes were stored when".
# ---------------------------------------------------------------------------


class Document(Base, TimestampMixin):
    """Metadata aggregate for a case-attached document.

    Why one row per document (vs. one row per version): the UI and the
    audit timeline both surface "the document" as a stable entity.
    Version history is a separate query against ``document_versions``.
    """

    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
    )
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    classification: Mapped[str] = mapped_column(String(64), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(32), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    uploaded_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    # P03-US8 lock flag. When True, the document refuses new versions
    # and any further redactions (the redaction use case enforces the
    # same rule; see ``protect_document.handle``).
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)

    __table_args__ = (
        Index("ix_documents_tenant_case", "tenant_id", "case_id"),
        # C2: detecting duplicates within the same case by hash.
        Index(
            "ix_documents_tenant_case_sha",
            "tenant_id",
            "case_id",
            "sha256",
        ),
    )


class DocumentVersion(Base):
    """An immutable per-version record of a document's bytes.

    Prior versions are preserved on a new-version upload (P03-US2,
    FR-006). The domain treats the rows as append-only; the SQL
    adapter never issues UPDATE on this table in v1.
    """

    __tablename__ = "document_versions"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "version_number",
            name="uq_document_versions_doc_version",
        ),
        Index("ix_document_versions_tenant_doc", "tenant_id", "document_id"),
    )


class DocumentRedaction(Base):
    """A versioned redaction of a document (P03-US6 / FM-025).

    The redaction references the document it applies to and the
    ``base_version_number`` it was derived from. The redacted bytes
    are content-addressed via ``sha256`` + ``storage_ref`` in the
    ``DocumentStore``; the original ``Document`` and ``DocumentVersion``
    rows are untouched so the original bytes remain retrievable.

    Why a separate table (vs a column on ``documents``): a document
    accumulates redactions over time and the timeline needs each one
    as a discrete auditable event. Storing them in a child table keeps
    the audit + outbox rows + redaction row in one transaction without
    overloading the parent.

    Status values: ACTIVE (the current redaction on the document) or
    SUPERSEDED (a later redaction has replaced it on the same
    document). Both are kept for audit replay; SUPERSEDED rows are
    never deleted.
    """

    __tablename__ = "document_redactions"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    base_version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)

    __table_args__ = (
        Index(
            "ix_document_redactions_tenant_doc",
            "tenant_id",
            "document_id",
            "created_at",
        ),
        Index(
            "ix_document_redactions_tenant_doc_status",
            "tenant_id",
            "document_id",
            "status",
        ),
    )
