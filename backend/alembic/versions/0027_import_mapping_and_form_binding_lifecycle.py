"""Add publish fields to import_mappings (FM-026).

Portable DDL (generic types only). Adds:
- import_mappings: published_at, published_by columns

Reversible: ``downgrade`` drops the columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_import_mapping_publish_fields"
down_revision: str | None = "0026_create_legal_holds_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("import_mappings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("published_by", sa.Uuid(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("import_mappings", schema=None) as batch_op:
        batch_op.drop_column("published_by")
        batch_op.drop_column("published_at")
