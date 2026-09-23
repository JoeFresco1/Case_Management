"""Per-tenant reference format overrides (FM-009 / P01-US9).

One row per tenant carrying the human-readable reference template the
``CaseRepository.next_reference`` adapter renders for new cases. An
absent row means "platform default" (``C-{TENANT}-{YYYY}-{SEQ}``).

Portable DDL (generic types only, no JSONB/GIN). Reversible:
``downgrade`` drops the index then the table. Tenants that configured
a custom template lose the override on downgrade — case references
already issued keep their strings (they live on ``cases.reference``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_reference_formats"
down_revision: str | None = "0020_queue_version_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reference_formats",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_reference_formats_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reference_formats"),
        sa.UniqueConstraint("tenant_id", name="uq_reference_formats_tenant"),
    )
    op.create_index("ix_reference_formats_tenant", "reference_formats", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_reference_formats_tenant", table_name="reference_formats")
    op.drop_table("reference_formats")
