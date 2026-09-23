"""create queues table

Foundation for P02-US3 / FM-016 (worker queue read with claim/release
and round-robin assignment) and P02-US9 (delegation).

# Schema

- ``queues`` — one row per configured work container. Tenant-scoped
  (AGENTS.md §3 R1, §12). All column types are SQLAlchemy generic
  (``Uuid``, ``JSON``, ``DateTime(timezone=True)``, ``String``,
  ``Integer``) so the same DDL works on SQLite (tests) and
  PostgreSQL / Azure SQL (production). No JSONB, no GIN, no
  ``gen_random_uuid()``.

  Indexes:

  - ``(tenant_id, key)`` — unique. The workflow engine references
    queues by key; uniqueness is the contract.

  - ``(tenant_id,)`` — supports the ``list_keys`` /
    ``list_queues`` admin query.

  ``member_user_ids`` is stored as a JSON list of UUID strings so
the round-robin order is read in one query and the column is
portable (AGENTS.md §4.R1 — no JSONB / arrays / range types).
``rr_cursor`` is the integer round-robin index into
``member_user_ids``; it advances on every auto-claim and is reset
to ``0`` when members are reconfigured.

# Reversibility

``downgrade`` drops the new index, then drops the new table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_create_queues"
down_revision: str | None = "0013_create_sla_clocks_and_legal_hold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "queues",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "assignment_policy",
            sa.String(length=16),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "member_user_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("rr_cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_queues_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_queues"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_queues_tenant_key"),
    )
    op.create_index("ix_queues_tenant", "queues", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_queues_tenant", table_name="queues")
    op.drop_table("queues")
