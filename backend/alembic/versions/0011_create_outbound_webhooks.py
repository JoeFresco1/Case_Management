"""create webhook_subscriptions and outbound_deliveries tables

Foundation for P13-US10 / FM-056 — authenticated, retry-safe outbound
webhook deliveries. The first slice ships the schema, the domain
entity, the application service, and the HTTP router; the
``/api/v1/webhooks/subscriptions`` endpoints are mounted in
``app.main``.

# Schema

Two tables, both tenant-scoped (AGENTS.md §3 R1, §12). Every column
uses SQLAlchemy generic types so the same DDL works on SQLite (tests)
and PostgreSQL / Azure SQL (production):

- ``webhook_subscriptions`` — one row per authorized subscriber. The
  ``url`` is the HTTPS endpoint deliveries are POSTed to; the
  ``event_filters_json`` is the catalog filter; ``secret_name`` is the
  logical key into the ``SecretProvider`` (no secret material on the
  row).
- ``outbound_deliveries`` — one row per (subscription, event) pair.
  The unique constraint on
  ``(tenant_id, subscription_id, event_id)`` is the database-level
  content-idempotency anchor (mirrors the inbound pattern in
  ``captured_webhooks`` and ``captured_emails``). The row carries the
  wire-shaped event payload so a replay can reconstruct the exact
  request body without re-reading the outbox.

# Idempotency

Two layers compose, both distinct from the HTTP ``Idempotency-Key``
middleware (AGENTS.md §10):

1. The unique constraint on ``outbound_deliveries`` ensures the same
   event is never enqueued twice for the same subscription.
2. The ``OutboundDeliveryTransport`` signs every delivery with
   ``X-Webhook-Signature: sha256=<hex>`` over ``f"{timestamp}.{body}"``
   so a subscriber can verify the body. A subscriber seeing the same
   ``X-Webhook-Event-Id`` twice knows it is a retry, not a new event.

# Reversibility

``downgrade`` drops the indexes, then the tables, in reverse order.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_create_outbound_webhooks"
down_revision: str | None = "0010_create_captured_emails"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("event_filters_json", sa.JSON(), nullable=False),
        sa.Column("secret_name", sa.String(length=128), nullable=False),
        sa.Column("signing_algorithm", sa.String(length=32), nullable=False),
        sa.Column("require_bearer", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version_token", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("description", sa.String(length=512), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_webhook_subscriptions_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_subscriptions"),
    )
    op.create_index(
        "ix_webhook_subscriptions_tenant_status",
        "webhook_subscriptions",
        ["tenant_id", "status"],
    )
    op.create_table(
        "outbound_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.String(length=64), nullable=True),
        sa.Column("target_url", sa.String(length=2048), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("first_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_outbound_deliveries_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["webhook_subscriptions.id"],
            ondelete="CASCADE",
            name="fk_outbound_deliveries_subscription_id_webhook_subscriptions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbound_deliveries"),
        sa.UniqueConstraint(
            "tenant_id",
            "subscription_id",
            "event_id",
            name="uq_outbound_deliveries_tenant_sub_event",
        ),
    )
    op.create_index(
        "ix_outbound_deliveries_tenant_state_created",
        "outbound_deliveries",
        ["tenant_id", "state", "created_at"],
    )
    op.create_index(
        "ix_outbound_deliveries_subscription_state",
        "outbound_deliveries",
        ["subscription_id", "state"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbound_deliveries_subscription_state", table_name="outbound_deliveries")
    op.drop_index(
        "ix_outbound_deliveries_tenant_state_created",
        table_name="outbound_deliveries",
    )
    op.drop_table("outbound_deliveries")
    op.drop_index(
        "ix_webhook_subscriptions_tenant_status",
        table_name="webhook_subscriptions",
    )
    op.drop_table("webhook_subscriptions")
