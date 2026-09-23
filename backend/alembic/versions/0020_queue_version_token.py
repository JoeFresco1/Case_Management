"""Add optimistic-concurrency token to queues (FM-016).

The ``Queue`` domain aggregate bumps ``version_token`` on every mutation
(rename / policy / members / cursor advance). The SQL repository enforces
compare-and-swap on this column so concurrent admin edits conflict instead
of silently overwriting.

Portable ``ADD COLUMN`` with a server default of 1 so pre-existing rows
(and fresh SQLite test DBs built from migrations) carry the initial token.
Downgrade drops the column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_queue_version_token"
down_revision: str | None = "0019_outbox_next_attempt_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("queues") as batch:
        batch.add_column(
            sa.Column(
                "version_token",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("queues") as batch:
        batch.drop_column("version_token")
