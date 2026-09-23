"""Widen cases.reference for tenant-configured templates (FM-009 / P01-US9).

Custom reference templates render up to ``MAX_RENDERED_LENGTH`` (200)
chars; the legacy ``String(64)`` column cannot hold them. Widens to
``String(200)`` with portable ``ALTER COLUMN`` semantics (batch mode
so SQLite rewrites the table; a plain alter on PostgreSQL).

Reversible: ``downgrade`` narrows back to ``String(64)``. Tenants that
issued >64-char references under a custom template must reset their
template and re-issue before downgrading — the narrowing is
lossy-adjacent by nature and downgrade refuses silently long rows
only via the database's own length check.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_widen_case_reference"
down_revision: str | None = "0021_reference_formats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        batch.alter_column(
            "reference",
            existing_type=sa.String(length=64),
            type_=sa.String(length=200),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("cases") as batch:
        batch.alter_column(
            "reference",
            existing_type=sa.String(length=200),
            type_=sa.String(length=64),
            existing_nullable=False,
        )
