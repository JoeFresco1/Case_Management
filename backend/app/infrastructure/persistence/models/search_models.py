"""Search ORM models: saved searches and search-audit rows (P06 / FM-032).

Split verbatim from the pre-split ``persistence/models.py`` (AGENTS.md §4.R2).
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
    UniqueConstraint,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# ---------------------------------------------------------------------------
# Saved searches + search audit (P06-US3, US5, US6 / FM-032).
#
# Two tables, both tenant-scoped:
#
# - ``saved_searches`` — one row per named, reusable query. Owner-
#   scoped via ``owner_user_id``; the unique constraint on
#   ``(tenant_id, owner_user_id, name)`` keeps the worker from saving
#   two rows with the same name. The ``query_shape`` column is a JSON
#   snapshot of the canonical ``{text_query, filters, sort}`` dict the
#   use case computed at save time; the audit row stores the same
#   dict so the security report can correlate "saved" and "ran".
# - ``search_audit`` — append-only audit row written every time a
#   saved search is saved or re-executed. The row carries the actor,
#   the saved-search id (nullable for ad-hoc audit extension), the
#   canonical query shape, the result count, and the timestamp. The
#   table is append-only at every layer; corrections are compensating
#   rows in the standard ``audit_events`` chain (not here).
#
# Tenant isolation is mandatory (AGENTS.md §3 R1, §12): both tables
# carry ``tenant_id`` and every SQL query includes ``WHERE
# tenant_id = :tenant_id``. Cross-tenant reads return ``None``.
# ---------------------------------------------------------------------------


class SavedSearch(Base):
    """A named, reusable case-search query (P06-US3 / FM-032).

    The row carries the canonical query shape so re-execution and the
    audit row are both reproducible from the persisted JSON. Owner-
    scoped: a different owner's row is invisible to the actor (the
    use case enforces owner-only reuse).

    Why a separate table from ``audit_events``: the row is the
    worker's saved work, not security evidence. It lives next to the
    data it queries (operationally: the case workspace) and is read
    by the worker, not by the security report.
    """

    __tablename__ = "saved_searches"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    text_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    filters_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False, default=list)
    sort_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False, default=list)
    # The canonical query shape snapshot. The audit row carries the
    # same dict so the security report can correlate saves and runs.
    query_shape: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "name",
            name="uq_saved_searches_tenant_owner_name",
        ),
        Index(
            "ix_saved_searches_tenant_owner",
            "tenant_id",
            "owner_user_id",
        ),
        Index(
            "ix_saved_searches_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )


class SearchAudit(Base):
    """Append-only audit row for saved-search save and run events (P06-US6 / FM-032).

    The row lives in its own table (not the global ``audit_events``
    chain) because the query shape and the result count are domain-
    specific and reading them through the global timeline would
    surface noise on every case's audit view. The row is the read
    surface for the security report at
    ``GET /api/v1/search_audit`` (P06-US5).

    Append-only is enforced at three layers (AGENTS.md §4.R12 / plan
    §4.R12): no UPDATE/DELETE in the application path, no migration
    rewrites, ORM listener (the same ``audit_immutability`` family
    can be extended here). Corrections are compensating rows in the
    global ``audit_events`` chain — a separate, standard audit
    correction row references the original ``search_audit`` id.
    """

    __tablename__ = "search_audit"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    # Nullable so a future ad-hoc-search audit row (not yet wired)
    # can carry ``None`` without breaking the column shape. A saved
    # search's save + run events always set this column to the row's
    # id.
    saved_search_id: Mapped[UUID | None] = mapped_column(
        SAUuid(),
        ForeignKey("saved_searches.id", ondelete="CASCADE"),
        nullable=True,
    )
    query_shape: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_search_audit_tenant_occurred",
            "tenant_id",
            "occurred_at",
        ),
        Index(
            "ix_search_audit_tenant_actor",
            "tenant_id",
            "actor_id",
        ),
        Index(
            "ix_search_audit_tenant_saved",
            "tenant_id",
            "saved_search_id",
        ),
    )
