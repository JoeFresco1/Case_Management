"""create saved_searches and search_audit tables

Foundation for P06-US3, US5, US6 / FM-032 — saved case searches and
their append-only audit trail. The first slice ships the schema, the
domain entity, the application use case, the SQL adapter, the HTTP
router, and the security-staff audit endpoint.

# Schema

Two tables, both tenant-scoped (AGENTS.md §3 R1, §12). Every column
uses SQLAlchemy generic types so the same DDL works on SQLite (tests)
and PostgreSQL / Azure SQL (production):

- ``saved_searches`` — one row per named, reusable query. The
  ``query_shape`` column is a JSON snapshot of the canonical
  ``{text_query, filters, sort}`` dict the use case computed at save
  time; the audit row stores the same dict so the security report
  can correlate "saved" and "ran".
- ``search_audit`` — append-only audit row written every time a
  saved search is saved or re-executed. The row carries the actor,
  the saved-search id (nullable so the same shape extends to ad-hoc
  audit later), the canonical query shape, the result count, and the
  timestamp.

# Idempotency + uniqueness

The unique constraint on ``(tenant_id, owner_user_id, name)`` keeps
the worker from saving two rows with the same name. The application
layer can treat the second save as a "rename" request in a later
slice; for now a duplicate name raises IntegrityError which the
router translates to 409.

# Reversibility

``downgrade`` drops the indexes, then the tables, in reverse order.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_create_saved_searches"
down_revision: str | None = "0011_create_outbound_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("text_query", sa.Text(), nullable=True),
        sa.Column("filters_json", sa.JSON(), nullable=False),
        sa.Column("sort_json", sa.JSON(), nullable=False),
        sa.Column("query_shape", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_saved_searches_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_saved_searches_owner_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_saved_searches"),
        sa.UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "name",
            name="uq_saved_searches_tenant_owner_name",
        ),
    )
    op.create_index(
        "ix_saved_searches_tenant_owner",
        "saved_searches",
        ["tenant_id", "owner_user_id"],
    )
    op.create_index(
        "ix_saved_searches_tenant_created",
        "saved_searches",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "search_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("saved_search_id", sa.Uuid(), nullable=True),
        sa.Column("query_shape", sa.JSON(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_search_audit_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["saved_search_id"],
            ["saved_searches.id"],
            ondelete="CASCADE",
            name="fk_search_audit_saved_search_id_saved_searches",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_search_audit"),
    )
    op.create_index(
        "ix_search_audit_tenant_occurred",
        "search_audit",
        ["tenant_id", "occurred_at"],
    )
    op.create_index(
        "ix_search_audit_tenant_actor",
        "search_audit",
        ["tenant_id", "actor_id"],
    )
    op.create_index(
        "ix_search_audit_tenant_saved",
        "search_audit",
        ["tenant_id", "saved_search_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_search_audit_tenant_saved", table_name="search_audit")
    op.drop_index("ix_search_audit_tenant_actor", table_name="search_audit")
    op.drop_index("ix_search_audit_tenant_occurred", table_name="search_audit")
    op.drop_table("search_audit")

    op.drop_index("ix_saved_searches_tenant_created", table_name="saved_searches")
    op.drop_index("ix_saved_searches_tenant_owner", table_name="saved_searches")
    op.drop_table("saved_searches")
