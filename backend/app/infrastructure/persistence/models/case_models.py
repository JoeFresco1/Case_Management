"""Case-domain ORM models.

Tenants, users, case types (+versions), cases, legal holds, participants, and
per-tenant reference formats. Split verbatim from the pre-split
``persistence/models.py`` (AGENTS.md §4.R2); columns, constraints, and
relationships are unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

from .base import TimestampMixin


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    is_system: Mapped[bool] = mapped_column(default=False, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_users_tenant_key"),)


class CaseType(Base, TimestampMixin):
    __tablename__ = "case_types"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sensitivity_default: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    current_version_id: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_case_types_tenant_key"),
        Index("ix_case_types_tenant", "tenant_id"),
    )

    versions: Mapped[list[CaseTypeVersion]] = relationship(
        back_populates="case_type", cascade="all, delete-orphan"
    )


class CaseTypeVersion(Base):
    __tablename__ = "case_type_versions"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    case_type_id: Mapped[UUID] = mapped_column(
        SAUuid(),
        ForeignKey("case_types.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    document_classifications: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("case_type_id", "version_number", name="uq_case_type_versions_num"),
        Index("ix_case_type_versions_tenant", "tenant_id"),
        Index(
            "ix_case_type_versions_case_type_status",
            "case_type_id",
            "status",
        ),
        # FM-021 inspect: tenant-scoped history lookup by
        # (tenant, case type, version) without scanning the status
        # index. Portable B-tree; added in 0028.
        Index(
            "ix_case_type_versions_tenant_case_version",
            "tenant_id",
            "case_type_id",
            "version_number",
        ),
    )

    case_type: Mapped[CaseType] = relationship(back_populates="versions")


class Case(Base, TimestampMixin):
    __tablename__ = "cases"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # FM-009: up to 200 chars so tenant-configured templates (bounded by
    # ``ReferenceFormat.MAX_RENDERED_LENGTH``) always fit. The legacy
    # default renders ~22 chars; the headroom is for custom literals.
    reference: Mapped[str] = mapped_column(String(200), nullable=False)
    case_type_version_id: Mapped[UUID] = mapped_column(
        SAUuid(),
        ForeignKey("case_type_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    current_workflow_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version_token: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # P08-US1 / FM-039 H0 seam: a single binary flag that, when True,
    # blocks any attempt to close the case (``change_status`` to a
    # terminal status). H1 will flesh the lifecycle: a per-case hold
    # row with actor / reason / released_at / released_by; for now
    # the flag is the only signal the application layer needs to
    # refuse the disposition (P08 §2). The column is non-null with a
    # False default so a migration is a one-line ALTER TABLE ADD
    # COLUMN ... DEFAULT FALSE NOT NULL.
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "reference", name="uq_cases_tenant_ref"),
        Index("ix_cases_tenant_status", "tenant_id", "status"),
    )


class LegalHold(Base, TimestampMixin):
    """Per-case legal-hold record (P08-US1, US2 / FM-039).

    H1 replaces the H0 boolean ``cases.legal_hold`` flag with a durable
    hold record that carries the placing actor / reason / timestamp and
    the releasing actor / reason / timestamp. The flag on ``cases`` stays
    as a denormalized read-side cache (set by the application use case).

    Identity is ``id`` (UUID4). The business key is ``(tenant_id,
    case_id)`` — at most one row per case; an active hold is the row
    with ``is_active=True`` (``released_at IS NULL``). Releasing a hold
    updates the existing row in place (sets ``released_*`` fields and
    flips ``is_active=False``), preserving the audit trail without
    breaking the unique constraint. A second concurrent ``place`` against
    the same case collides on the unique constraint and surfaces as
    ``Conflict``.
    """

    __tablename__ = "legal_holds"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False
    )
    placed_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    placed_reason: Mapped[str] = mapped_column(Text, nullable=False)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    released_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", name="uq_legal_holds_tenant_case"),
        Index("ix_legal_holds_tenant_case", "tenant_id", "case_id"),
        Index("ix_legal_holds_tenant_active", "tenant_id", "is_active"),
    )


class Participant(Base, TimestampMixin):
    """SQL row for a case participant, including removal tombstones."""

    __tablename__ = "participants"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_participants_tenant", "tenant_id"),
        Index("ix_participants_case", "tenant_id", "case_id"),
    )


# ---------------------------------------------------------------------------
# Reference formats (P01 / FM-009 / P01-US9)
#
# One row per tenant carrying the human-readable reference template the
# ``CaseRepository.next_reference`` adapter renders for new cases. Absent
# row means "platform default" (``C-{TENANT}-{YYYY}-{SEQ}``). The template
# string is validated by ``app.domain.reference_format.validate_template``
# at the use-case boundary, never here. Existing ``cases.reference``
# rows are never rewritten — the internal ``Case.id`` UUID is the
# immutable identity; the reference is one projection of it.
# ---------------------------------------------------------------------------


class ReferenceFormat(Base, TimestampMixin):
    """Per-tenant case reference template override (FM-009)."""

    __tablename__ = "reference_formats"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    template: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_reference_formats_tenant"),
        Index("ix_reference_formats_tenant", "tenant_id"),
    )
