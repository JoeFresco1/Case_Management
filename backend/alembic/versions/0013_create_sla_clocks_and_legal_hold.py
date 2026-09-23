"""create sla_clocks table and add cases.legal_hold column

Foundation for P02-US6 / FM-018 (SLA clocks + breach audit) and
P08-US1 / FM-039 (legal-hold refusal seam, H0).

# Schema

- ``sla_clocks`` — one row per deadline the workflow engine
  started. Tenant-scoped (AGENTS.md §3 R1, §12). All column types
  are SQLAlchemy generic (``Uuid``, ``JSON``, ``DateTime(timezone=True)``,
  ``String``) so the same DDL works on SQLite (tests) and
  PostgreSQL / Azure SQL (production). No JSONB, no GIN, no
  ``gen_random_uuid()``.

  Indexes:

  - ``(tenant_id, case_id)`` — per-case timeline lookup.
  - ``(tenant_id, due_at)`` — scanner tick range scan.
  - ``(tenant_id, status, due_at)`` — scanner's "running clocks
    whose deadline has passed" hot path.

  No ``current_state``-style index on status alone; the
  multi-column index is enough at the slice's expected scale and
  avoids the "single-column index that duplicates the
  multi-column prefix" footgun.

- ``cases.legal_hold`` — boolean NOT NULL with default FALSE. H0
  refuses any terminal status flip while the flag is TRUE; H1
  will add a per-case hold record (actor / reason / released_at /
  released_by) and the column becomes a denormalized read-side
  cache. Adding the column now is cheap (default-FALSE NOT NULL
  is metadata-only on PostgreSQL ≥ 11) and keeps the H0 refusal
  guarded at the aggregate.

# Reversibility

``downgrade`` drops the new index, drops the new column, and
drops the new table, in reverse order.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_create_sla_clocks_and_legal_hold"
down_revision: str | None = "0012_create_saved_searches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # FM-039 H0: legal-hold refusal seam. Boolean NOT NULL with
    # default FALSE so existing rows are valid without a backfill
    # step and the column's nullable-ness is fixed in the schema.
    op.add_column(
        "cases",
        sa.Column(
            "legal_hold",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # FM-018 / P02-US6: sla_clocks table.
    op.create_table(
        "sla_clocks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("clock_key", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("breach_action", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_sla_clocks_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
            ondelete="CASCADE",
            name="fk_sla_clocks_case_id_cases",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sla_clocks"),
    )
    op.create_index(
        "ix_sla_clocks_tenant_case",
        "sla_clocks",
        ["tenant_id", "case_id"],
    )
    op.create_index(
        "ix_sla_clocks_tenant_due",
        "sla_clocks",
        ["tenant_id", "due_at"],
    )
    op.create_index(
        "ix_sla_clocks_tenant_status_due",
        "sla_clocks",
        ["tenant_id", "status", "due_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sla_clocks_tenant_status_due", table_name="sla_clocks")
    op.drop_index("ix_sla_clocks_tenant_due", table_name="sla_clocks")
    op.drop_index("ix_sla_clocks_tenant_case", table_name="sla_clocks")
    op.drop_table("sla_clocks")
    op.drop_column("cases", "legal_hold")
