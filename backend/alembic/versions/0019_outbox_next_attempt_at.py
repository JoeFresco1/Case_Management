"""Outbox retry backoff (T029).

Failures set ``next_attempt_at`` so a poison message backs off
instead of hot-spinning the drain loop. The delay is deterministic
(``min(cap, base * 2**(attempt-1))``) — no jitter, so scanner timing
stays reproducible in tests. ``NULL`` means "due immediately" and
covers every pre-existing row without a backfill.

Portable ``ADD COLUMN`` on SQLite and PostgreSQL; generic types only.
Downgrade drops the column (the dispatcher treats a missing column
as always-due — only valid pre-T029).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_outbox_next_attempt_at"
down_revision: str | None = "0018_audit_payload_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("outbox") as batch:
        batch.add_column(
            sa.Column(
                "next_attempt_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("outbox") as batch:
        batch.drop_column("next_attempt_at")
