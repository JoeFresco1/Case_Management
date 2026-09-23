"""Legal-holds durable table (FM-039 / P08-US1, US2).

H1 replaces the H0 boolean ``cases.legal_hold`` flag with a per-case
hold record carrying placed/released actor + reason + timestamp. The
flag on ``cases`` stays as a denormalized read-side cache.

Portable DDL (generic types only — AGENTS.md R1). No JSONB, no
gen_random_uuid(). Reversible: ``downgrade`` drops indexes, constraint,
then the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_create_legal_holds_table"
down_revision: str | None = "0025_import_mappings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "legal_holds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("placed_by", sa.Uuid(), nullable=False),
        sa.Column("placed_reason", sa.Text(), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_by", sa.Uuid(), nullable=True),
        sa.Column("released_reason", sa.Text(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_legal_holds_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
            ondelete="CASCADE",
            name="fk_legal_holds_case_id_cases",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_legal_holds"),
        sa.UniqueConstraint("tenant_id", "case_id", name="uq_legal_holds_tenant_case"),
    )
    op.create_index("ix_legal_holds_tenant_case", "legal_holds", ["tenant_id", "case_id"])
    op.create_index("ix_legal_holds_tenant_active", "legal_holds", ["tenant_id", "is_active"])


def downgrade() -> None:
    op.drop_index("ix_legal_holds_tenant_active", table_name="legal_holds")
    op.drop_index("ix_legal_holds_tenant_case", table_name="legal_holds")
    op.drop_table("legal_holds")
