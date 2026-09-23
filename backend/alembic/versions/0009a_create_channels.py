"""create tenant-scoped capture channels.

The captured-email revision depends on ``channels``. Keeping the channel
catalog in its own revision makes that dependency explicit and ensures the
foreign key can be created on PostgreSQL as well as SQLite.

All columns use SQLAlchemy generic types. The JSON policy is intentionally
not indexed; policy evaluation is application-side and the schema remains
portable to Azure SQL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009a_create_channels"
down_revision: str | None = "0009_create_document_redactions_and_lock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the tenant-scoped channel catalog required by capture flows."""
    op.create_table(
        "channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_channels_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("policy_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_channels"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_channels_tenant_key"),
    )
    op.create_index(
        "ix_channels_tenant_enabled",
        "channels",
        ["tenant_id", "enabled"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the channel index and table in reverse creation order."""
    op.drop_index("ix_channels_tenant_enabled", table_name="channels")
    op.drop_table("channels")
