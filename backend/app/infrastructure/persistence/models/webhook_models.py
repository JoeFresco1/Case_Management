"""Outbound webhook ORM models: subscriptions and delivery attempts.

The OUTBOUND half of the webhook story (FM-056 / P13-US10 / ADR-013),
deliberately separate from ``ingestion_models`` (inbound captures).
Split verbatim from the pre-split ``persistence/models.py`` (AGENTS.md §4.R2).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# ---------------------------------------------------------------------------
# Outbound webhook tables (FM-056 / P13-US10 / ADR-013).
#
# These two tables are the OUTBOUND half of the webhook story. They
# are deliberately separate from ``captured_webhooks`` (the INBOUND
# half, FM-030 / ADR-007): trust direction, idempotency anchor, and
# failure semantics are all inverted. See
# ``specs/pillars/01-deduplicated-feature-map.md:189`` and
# ``app.domain.outbound_webhook`` for the contrast.
# ---------------------------------------------------------------------------


class WebhookSubscription(Base):
    """An outbound webhook subscription (FM-056 / P13-US10).

    The row is the authorization anchor for delivery: a delivery is
    permitted only if the subscription is ``ACTIVE`` and the
    ``event_type`` is in ``event_filters_json``. The signing secret
    is resolved through the ``SecretProvider`` port; the column
    here stores the logical key, not the value.
    """

    __tablename__ = "webhook_subscriptions"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # JSON array of event_type strings. We use a JSON column rather
    # than a separate child table so the operator console can render
    # the filter set in a single read; the application layer
    # validates the array contents (catalog membership) at write
    # time.
    event_filters_json: Mapped[list[str]] = mapped_column(JSON(), nullable=False)
    secret_name: Mapped[str] = mapped_column(String(128), nullable=False)
    signing_algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    require_bearer: Mapped[bool] = mapped_column(default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version_token: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    __table_args__ = (
        # No UNIQUE on (tenant_id, url): a tenant may legitimately
        # register two subscriptions to the same URL with different
        # event filters or different secrets (e.g. one for production
        # traffic, one for replay). The application layer rejects
        # duplicate (url, event_filters) at registration if it wants
        # to prevent that; today it does not.
        Index("ix_webhook_subscriptions_tenant_status", "tenant_id", "status"),
    )


class OutboundDelivery(Base):
    """A single outbound webhook delivery attempt (FM-056 / P13-US10).

    The row is the durable record of "the platform tried to send
    event X to subscription Y". It is the retry anchor: the
    dispatcher reads pending rows, attempts the HTTP POST, and
    either marks the row succeeded or bumps the attempt counter.

    Idempotency is enforced at the database level: the unique
    constraint ``(tenant_id, subscription_id, event_id)`` ensures
    a second enqueue for the same event is a no-op. The
    application layer catches the ``IntegrityError`` and returns
    the prior row.

    The wire-shaped event payload is stored on the row so a replay
    can reconstruct the exact request body without re-reading the
    outbox. The envelope is immutable once written.
    """

    __tablename__ = "outbound_deliveries"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    subscription_id: Mapped[UUID] = mapped_column(
        SAUuid(),
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSON(), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(default=0, nullable=False)
    last_status_code: Mapped[int | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "subscription_id",
            "event_id",
            name="uq_outbound_deliveries_tenant_sub_event",
        ),
        Index(
            "ix_outbound_deliveries_tenant_state_created",
            "tenant_id",
            "state",
            "created_at",
        ),
        Index(
            "ix_outbound_deliveries_subscription_state",
            "subscription_id",
            "state",
        ),
    )
