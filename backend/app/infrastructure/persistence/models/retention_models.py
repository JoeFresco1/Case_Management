"""Retention ORM models: retention policies and disposition requests
(P08-US3, US4 / FM-040). Split verbatim from the pre-split
``persistence/models.py`` (AGENTS.md §4.R2).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# ---------------------------------------------------------------------------
# Retention policies + disposition requests (P08-US3, US4 / FM-040).
#
# The retention pillar ships two related tables:
#
# - ``retention_policies`` — the versioned, immutable configuration
#   artefact (one row per published version). The aggregate's
#   ``publish()`` / ``retire()`` transitions are enforced at the
#   domain layer; the row carries the lifecycle status so the SQL
#   adapter can index it for the eligibility scan.
# - ``disposition_requests`` — the per-case review queue surfaced
#   when a case crosses its retention horizon. Lifecycle is
#   ``pending`` -> ``approved`` | ``rejected``; both terminal states
#   are auditable in the same row via ``reviewed_by`` /
#   ``reviewed_at`` / ``review_reason``.
#
# Tenant isolation is mandatory on both tables (AGENTS.md §3 R1,
# §12); the application layer carries ``tenant_id`` on every
# repository method and the SQL adapter MUST include ``WHERE
# tenant_id = :tenant_id`` on every query.
# ---------------------------------------------------------------------------


class RetentionPolicy(Base):
    """A versioned retention declaration (P08-US3 / FM-040).

    One row per published version of a policy. The aggregate is
    immutable once ``status='published'``; ``retired`` rows remain in
    the table so an auditor can replay which policy was active when
    a record was created.

    The retention ``period`` is stored as a portable ``Integer`` day
    count — the application layer translates ``timedelta`` <-> days
    on the boundary. Days is the only resolution the eligibility
    scan needs (FM-040 acceptance: "LabResult documents, 7 years."
    — the precision loss of days-vs-seconds is irrelevant for
    horizon arithmetic). This keeps the column portable across
    SQLite (tests) and PostgreSQL (local).
    """

    __tablename__ = "retention_policies"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    target_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # Why integer days: AGENTS.md §4.R1 — no dialect-specific types.
    # The domain ``timedelta`` is converted at the persistence
    # boundary (see ``mapping.retention_to_domain`` / ``..._from_domain``).
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "category",
            "target_key",
            "version_number",
            name="uq_retention_policies_version",
        ),
        Index("ix_retention_policies_tenant_target", "tenant_id", "category", "target_key"),
        Index("ix_retention_policies_tenant_status", "tenant_id", "status"),
    )


class DispositionRequest(Base):
    """A per-case disposition review (P08-US4 / FM-040).

    Surfaced by the eligibility scan; an approver either approves or
    rejects it. The row is durable and audited; the actual case
    destruction is a follow-up dispatch (out of scope for v1).

    A case has at most one **pending** row at a time. The
    ``uq_disposition_requests_pending`` partial index (modeled via
    the application-layer pre-check; v1 leaves the SQL adapter with
    a uniqueness check the application enforces via
    ``get_pending_for_case``) is the defense-in-depth layer that
    prevents a duplicate surfacing (Constitution V).
    """

    __tablename__ = "disposition_requests"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    case_id: Mapped[UUID] = mapped_column(
        SAUuid(),
        ForeignKey("cases.id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_id: Mapped[UUID] = mapped_column(
        SAUuid(),
        ForeignKey("retention_policies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    surfaced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    surfaced_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    reviewed_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_disposition_requests_tenant_case_status",
            "tenant_id",
            "case_id",
            "status",
        ),
        Index(
            "ix_disposition_requests_tenant_status",
            "tenant_id",
            "status",
        ),
    )
