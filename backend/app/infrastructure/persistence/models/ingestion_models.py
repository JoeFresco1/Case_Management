"""Ingestion ORM models: imports, channels, quarantine, and captures.

Import jobs/mappings (P14 / FM-026), channels + quarantine (P04-US4/US5),
and captured emails/webhooks (P04-US6/US8 / FM-028 / FM-030). Split verbatim
from the pre-split ``persistence/models.py`` (AGENTS.md §4.R2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

from .base import TimestampMixin

# ---------------------------------------------------------------------------
# Import jobs (P14-US2, US3 / P04-US3 / AGENTS.md §16.1).
#
# The relational row carries the bookkeeping (status, source label,
# counts, correlation). The reconciliation report is stored as JSON so
# future schemas (rejected row reasons, row indices) do not require a
# migration. The ``mapping_json`` snapshot is intentionally denormalised
# so an old import is replayed against the mapping it was committed
# with — no surprises from a later version bump (Constitution XI).
# ---------------------------------------------------------------------------


class ImportJob(Base, TimestampMixin):
    """A bounded ingestion from an approved source.

    Why one row per job: the operator previews the reconciliation on the
    ``ImportJob`` aggregate, then commits the same job. Storing per-row
    outcomes inside the same row keeps the preview/commit cycle atomic
    with no extra join at the API boundary (the operator console pulls
    a single row to render the report).
    """

    __tablename__ = "import_jobs"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    source_label: Mapped[str] = mapped_column(String(200), nullable=False)
    source_format: Mapped[str] = mapped_column(String(16), nullable=False)
    source_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mapping_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reconciliation_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    results_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (
        Index("ix_import_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_import_jobs_tenant_created", "tenant_id", "created_at"),
    )


class ImportMapping(Base, TimestampMixin):
    """Versioned import-mapping registry row (P14-US3 / FM-026).

    Identity is ``(tenant_id, key, version_number)``; lifecycle mirrors
    ``CaseTypeVersion`` (draft → published → retired). Published rows
    are immutable; retired rows block new jobs but old jobs keep their
    inline snapshot on ``import_jobs.mapping_json``.
    """

    __tablename__ = "import_mappings"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    mapping_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "key", "version_number", name="uq_import_mappings_tenant_key_version"
        ),
        Index("ix_import_mappings_tenant_key", "tenant_id", "key"),
        Index("ix_import_mappings_tenant_status", "tenant_id", "status"),
    )


# ---------------------------------------------------------------------------
# Channels + quarantine (P04-US4, US5 / FM-027).
# ---------------------------------------------------------------------------


class Channel(Base, TimestampMixin):
    """A tenant-scoped ingestion channel.

    The ``key`` is the operator-facing identity (stable, lowercase
    alnum + underscore + dash). The ``policy_json`` blob carries the
    :class:`ValidationPolicy` fields verbatim so the operator UI can
    re-render it without a join. ``enabled`` defaults to True so a
    freshly drafted channel accepts material immediately; ``disable``
    is the explicit operator action.
    """

    __tablename__ = "channels"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_channels_tenant_key"),
        Index("ix_channels_tenant_enabled", "tenant_id", "enabled"),
    )


class QuarantineRecord(Base, TimestampMixin):
    """A captured item held for operator review.

    The original row payload is **not** stored on this row — the
    diagnostic ``context`` dict carries keys only (``missing``,
    ``unknown``), never values. The status field drives the operator
    queue; ``RELEASED`` / ``REJECTED`` are terminal.
    """

    __tablename__ = "quarantine_records"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    channel_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_label: Mapped[str] = mapped_column(String(200), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        Index(
            "ix_quarantine_tenant_status_captured",
            "tenant_id",
            "status",
            "captured_at",
        ),
        Index("ix_quarantine_tenant_channel", "tenant_id", "channel_id"),
    )


class CapturedEmail(Base):
    """Provenance row for one captured inbound email (P04-US6 / FM-028).

    The row carries the *identity* (RFC 5322 ``Message-Id``) and the
    *artifact* (the document or case-note id it produced). The email
    body lives on the artifact storage path; this row is the durable
    dedup ledger keyed on ``(tenant_id, channel_id, message_id)``.

    The unique constraint is the content idempotency anchor: a second
    submission of the same Message-Id collides on insert and the
    application layer returns the prior row without producing a
    second artifact. This is independent of the HTTP
    ``Idempotency-Key`` middleware (AGENTS.md §10).
    """

    __tablename__ = "captured_emails"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    channel_key: Mapped[str] = mapped_column(String(64), nullable=False)
    message_id: Mapped[str] = mapped_column(String(998), nullable=False)
    sender: Mapped[str] = mapped_column(String(320), nullable=False)
    recipients_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    destination: Mapped[str] = mapped_column(String(16), nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_id",
            "message_id",
            name="uq_captured_emails_tenant_channel_message",
        ),
        Index(
            "ix_captured_emails_tenant_captured_at",
            "tenant_id",
            "captured_at",
        ),
        Index(
            "ix_captured_emails_tenant_case",
            "tenant_id",
            "case_id",
        ),
    )


class CapturedWebhook(Base):
    """Provenance row for one captured inbound webhook (P04-US8 / FM-030).

    The row carries the *identity* (producer ``X-Webhook-Id``) and the
    *command* (the case command it dispatched). The original payload
    lives on this row as ``payload_json`` so the operator console can
    replay the event; the audit chain records only ``payload_digest``
    so a tampered payload row is detectable.

    The unique constraint on ``(tenant_id, channel_id, webhook_id)``
    is the content idempotency anchor: a second submission of the same
    webhook id collides on insert and the application layer returns
    the prior row without dispatching a second command. This mirrors
    ``captured_emails`` (FM-028) and is independent of the HTTP
    ``Idempotency-Key`` middleware (AGENTS.md R7).

    All column types are SQLAlchemy generic types (Uuid, JSON,
    DateTime(timezone=True), String) for Azure SQL portability
    (AGENTS.md R1). Every query must filter ``tenant_id``; a
    cross-tenant webhook id lookup returns None (Constitution V).
    """

    __tablename__ = "captured_webhooks"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    channel_key: Mapped[str] = mapped_column(String(64), nullable=False)
    webhook_id: Mapped[str] = mapped_column(String(998), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    command_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    case_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel_id",
            "webhook_id",
            name="uq_captured_webhooks_tenant_channel_webhook",
        ),
        Index(
            "ix_captured_webhooks_tenant_captured_at",
            "tenant_id",
            "captured_at",
        ),
        Index(
            "ix_captured_webhooks_tenant_case",
            "tenant_id",
            "case_id",
        ),
    )
