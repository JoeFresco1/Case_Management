"""create captured_emails table

Foundation for P04-US6 / FM-028 — inbound email capture with content
idempotency keyed on the RFC 5322 ``Message-Id``. The first slice
ships with synthetic fixtures only (AGENTS.md §18); live enterprise
email ingestion is gated behind the Graph / IMAP connector (P14).

# Schema

``captured_emails`` carries the provenance of one captured inbound
email:

- identity + bookkeeping: ``id``, ``tenant_id``, ``channel_id``,
  ``channel_key``, ``captured_at``, ``captured_by``, ``correlation_id``.
- message identity: ``message_id`` (RFC 5322, ≤998 chars), ``sender``,
  ``recipients_json`` (tuple serialized as JSON), ``subject``.
- artifact link: ``destination`` (``"document"`` today, ``"case_note"``
  reserved), ``artifact_id`` (UUID), ``artifact_kind``, ``case_id``
  (nullable for free-standing documents, not NULL for the first slice).

The email body is **not** persisted on this row — the bytes live on
the ``documents`` / ``document_versions`` rows this capture produced
(or the case-note path, deferred). This is the AGENTS.md §18 +
Constitution V redaction posture.

# Idempotency

The ``(tenant_id, channel_id, message_id)`` tuple is ``UNIQUE`` so a
re-submitted Message-Id can rely on database-level enforcement of
content idempotency, distinct from the HTTP ``Idempotency-Key``
header replay layer (AGENTS.md §10). The application layer translates
a duplicate-key collision into the prior ``CapturedEmail`` row.

# Portability

All column types are SQLAlchemy generic types (Uuid, JSON,
DateTime(timezone=True), Integer, String, Text). No
``gen_random_uuid()``, no JSONB, no exclusion constraints. Both
SQLite (tests) and PostgreSQL/Azure SQL accept the shape.

# Reversibility

``downgrade`` drops the indexes, then the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_create_captured_emails"
down_revision: str | None = "0009a_create_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "captured_emails",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("channel_key", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.String(length=998), nullable=False),
        sa.Column("sender", sa.String(length=320), nullable=False),
        sa.Column("recipients_json", sa.JSON(), nullable=False),
        sa.Column("subject", sa.String(length=998), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_by", sa.Uuid(), nullable=False),
        sa.Column("destination", sa.String(length=16), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_kind", sa.String(length=32), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_captured_emails_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            ondelete="CASCADE",
            name="fk_captured_emails_channel_id_channels",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_captured_emails"),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_id",
            "message_id",
            name="uq_captured_emails_tenant_channel_message",
        ),
    )
    op.create_index(
        "ix_captured_emails_tenant_captured_at",
        "captured_emails",
        ["tenant_id", "captured_at"],
    )
    op.create_index(
        "ix_captured_emails_tenant_case",
        "captured_emails",
        ["tenant_id", "case_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_captured_emails_tenant_case", table_name="captured_emails")
    op.drop_index("ix_captured_emails_tenant_captured_at", table_name="captured_emails")
    op.drop_table("captured_emails")
