"""Durable break-glass grants (FM-035 / P11-US10).

Grants are the only legitimate path that bypasses the standard tenant
filter. They must survive restarts (an in-memory store would silently
drop an active override — or worse, drop a revocation), so they live
in ``break_glass_grants`` keyed on ``(tenant_id, user_id, scope)``.

Portable DDL (generic types only). Reversible: ``downgrade`` drops the
index then the table. Downgrading while grants are active removes the
override records — active windows end immediately, which is the
fail-closed direction.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_break_glass_grants"
down_revision: str | None = "0022_widen_case_reference"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "break_glass_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("granted_by", sa.Uuid(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_break_glass_grants_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_break_glass_grants"),
    )
    op.create_index(
        "ix_break_glass_grants_tenant_user_scope",
        "break_glass_grants",
        ["tenant_id", "user_id", "scope"],
    )


def downgrade() -> None:
    op.drop_index("ix_break_glass_grants_tenant_user_scope", table_name="break_glass_grants")
    op.drop_table("break_glass_grants")
