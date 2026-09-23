"""Import mapping registry (FM-026 / P14-US3).

Portable DDL (generic types only). Reversible: ``downgrade`` drops the
indexes then the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_import_mappings"
down_revision: str | None = "0024_form_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("mapping_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_import_mappings_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_mappings"),
        sa.UniqueConstraint(
            "tenant_id", "key", "version_number", name="uq_import_mappings_tenant_key_version"
        ),
    )
    op.create_index("ix_import_mappings_tenant_key", "import_mappings", ["tenant_id", "key"])
    op.create_index("ix_import_mappings_tenant_status", "import_mappings", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_import_mappings_tenant_status", table_name="import_mappings")
    op.drop_index("ix_import_mappings_tenant_key", table_name="import_mappings")
    op.drop_table("import_mappings")
