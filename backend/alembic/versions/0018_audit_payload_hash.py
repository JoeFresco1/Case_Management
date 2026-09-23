"""Persist the audit payload hash (T030).

``record_event`` is idempotent on ``(tenant_id, event_type,
correlation_id, payload_hash)``, but the ``audit_events`` table had no
``payload_hash`` column — the SQL ``dedupe`` matched on the 3-tuple
only, so a same-key retry carrying a *different* payload collapsed
onto the first row and silently dropped the second event.

This migration adds ``payload_hash`` (canonical-payload SHA-256,
``domain.audit.payload_hash``). Pre-existing rows predate the writer
and get an empty-string backfill via ``server_default``; every writer
from this revision on sets the real hash, and the dedupe query matches
the full 4-tuple (empty backfill values never equal a real 64-hex
hash, so legacy rows can neither false-positive nor false-negative).

Portable ``ADD COLUMN`` on both SQLite and PostgreSQL; generic types
only. Downgrade drops the column (dedupe reverts to 3-tuple matching
— only valid pre-T030).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_audit_payload_hash"
down_revision: str | None = "0017_reconcile_document_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("audit_events") as batch:
        batch.add_column(
            sa.Column(
                "payload_hash",
                sa.String(length=64),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("audit_events") as batch:
        batch.drop_column("payload_hash")
