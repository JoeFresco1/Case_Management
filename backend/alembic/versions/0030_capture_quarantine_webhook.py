"""Capture persistence parity: quarantine records + inbound webhooks.

D-3 (spec 002-leftovers T1 / FM-027 + FM-030) completes the durable
capture ledger. ``models.py`` already declares ``ImportJob``
(import packages), ``ImportMapping`` (mapping versions), ``Channel``
(capture channels), ``QuarantineRecord`` (quarantine), and
``CapturedEmail`` — but the migration chain never created
``quarantine_records``, and the inbound-webhook half (``CapturedWebhook``)
had no ORM row at all (the SQL adapter raised ``NotImplementedError``).

This revision creates both missing tables in one step:

- ``quarantine_records`` — per-record holding area for malformed
  captures. One row per held item; the original payload is NOT stored
  (``context_json`` carries diagnostic keys only, Constitution V).
- ``captured_webhooks`` — durable provenance for inbound webhook
  commands, keyed on ``(tenant_id, channel_id, webhook_id)``.

Portable DDL (generic types only — AGENTS.md R1). No JSONB, no GIN,
no ILIKE, no tsvector, no gen_random_uuid(). Reversible:
``downgrade`` drops indexes then tables in reverse creation order.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_capture_quarantine_webhook"
down_revision: str | None = "0029_viewer_annotations_checkout_renditions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quarantine_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("channel_key", sa.String(length=64), nullable=False),
        sa.Column("source_label", sa.String(length=200), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_by", sa.Uuid(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_quarantine_records_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            ondelete="CASCADE",
            name="fk_quarantine_records_channel_id_channels",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quarantine_records"),
    )
    op.create_index(
        "ix_quarantine_tenant_status_captured",
        "quarantine_records",
        ["tenant_id", "status", "captured_at"],
    )
    op.create_index(
        "ix_quarantine_tenant_channel",
        "quarantine_records",
        ["tenant_id", "channel_id"],
    )

    op.create_table(
        "captured_webhooks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("channel_key", sa.String(length=64), nullable=False),
        sa.Column("webhook_id", sa.String(length=998), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("command_kind", sa.String(length=32), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_by", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_captured_webhooks_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            ondelete="CASCADE",
            name="fk_captured_webhooks_channel_id_channels",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_captured_webhooks"),
        sa.UniqueConstraint(
            "tenant_id",
            "channel_id",
            "webhook_id",
            name="uq_captured_webhooks_tenant_channel_webhook",
        ),
    )
    op.create_index(
        "ix_captured_webhooks_tenant_captured_at",
        "captured_webhooks",
        ["tenant_id", "captured_at"],
    )
    op.create_index(
        "ix_captured_webhooks_tenant_case",
        "captured_webhooks",
        ["tenant_id", "case_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_captured_webhooks_tenant_case", table_name="captured_webhooks")
    op.drop_index("ix_captured_webhooks_tenant_captured_at", table_name="captured_webhooks")
    op.drop_table("captured_webhooks")
    op.drop_index("ix_quarantine_tenant_channel", table_name="quarantine_records")
    op.drop_index("ix_quarantine_tenant_status_captured", table_name="quarantine_records")
    op.drop_table("quarantine_records")
