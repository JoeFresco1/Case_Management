"""Append-only operational ORM models.

Configuration publications, the audit hash-chain rows (incl. signed batches),
the durable outbox, and idempotency keys. Split verbatim from the pre-split
``persistence/models.py`` (AGENTS.md §4.R2).
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
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# ---------------------------------------------------------------------------
# Configuration publications (FM-021 / P10-US4, US5, US8 / AGENTS.md §16.1).
#
# Append-only durable trail of every configuration lifecycle action
# (draft / validate / publish / retire). Each row carries the actor, the
# optional change summary, and the artifact + version ids so the admin
# timeline (P10-US4) can render "who published what when" without joining
# the audit chain.
#
# The companion migration is 0006_create_configuration_and_idempotency
# — the table is created there; this model is the SQLAlchemy mapping
# that lets the application layer read/write the rows through the
# :class:`ConfigurationPublicationRepository` Protocol. The
# ``configuration_publications`` table is the durable read surface for
# ``GET /api/v1/admin/publications`` (FM-021).
#
# Why artifact_id and version_id are nullable: the artifact itself may
# be retired (status='retired') or even deleted later; the publication
# history outlives the artifact. The application tier resolves the
# artifact on read; the table only enforces the tenant boundary. This
# matches the migration comment block (plan §4.R12).
# ---------------------------------------------------------------------------


class ConfigurationPublication(Base):
    """Append-only configuration lifecycle row (FM-021 / P10-US4, US5, US8).

    The row carries the human-readable ``change_summary`` (the admin's
    note on the publish / retire action) and the durable linkage to the
    artifact + version that was acted on. It is the read surface for
    :func:`governance.list_publications` and the input to the
    second-admin approval gate (P10-US8): when ``require_second_admin``
    is on for the tenant, the publish action appends a ``draft``
    publication row, and a second administrator must append a matching
    ``publish`` row with a different ``actor_id`` before the version is
    considered effectively published.

    Status is preserved verbatim on the row so the admin timeline can
    distinguish a draft from a publish from a retire without joining
    :class:`AuditEvent`.
    """

    __tablename__ = "configuration_publications"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_id: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    version_id: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    # ``action`` is one of "draft" | "validate" | "publish" | "retire"
    # — the lifecycle vocabulary from P10 §2. Stored as String so a
    # future action ("archive") does not require a migration.
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Why Text: change_summary is human-authored and may be long; the
    # column is scan-friendly on sqlite and PostgreSQL.
    change_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_configuration_publications_tenant", "tenant_id"),
        Index(
            "ix_configuration_publications_artifact",
            "tenant_id",
            "artifact_type",
            "artifact_id",
        ),
        Index(
            "ix_configuration_publications_tenant_occurred",
            "tenant_id",
            "occurred_at",
        ),
        # FM-021 inspect/approval: look up the publish/validate chain
        # for one version id (second-admin gate + inspect join).
        # Portable B-tree; added in 0028.
        Index(
            "ix_configuration_publications_tenant_version",
            "tenant_id",
            "version_id",
        ),
    )


# ---------------------------------------------------------------------------
# Audit, outbox, idempotency — append-only / durable stores (FM-003, FM-043,
# AGENTS.md §12, §16.1).
# ---------------------------------------------------------------------------


class AuditEvent(Base):
    """Append-only audit row with chained SHA-256 (AGENTS.md §12, plan §4.R12).

    The hash chain is the tamper evidence: ``hash = SHA-256(prev_hash +
    canonical(payload_json))``. Corrections are compensating rows
    (`AuditCorrectionRecorded`) referencing the original; there is no
    UPDATE/DELETE on this table at any layer.
    """

    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    case_id: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    causation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # T030: canonical-payload SHA-256 (``domain.audit.payload_hash``),
    # independent of the chain anchor. The audit dedupe key is
    # ``(tenant_id, event_type, correlation_id, payload_hash)``: the
    # chain ``hash`` binds ``prev_hash`` too, so it cannot serve as
    # the dedupe key (same payload at different chain positions
    # hashes differently). Always written by ``SqlAuditEventStore``.
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    __table_args__ = (
        Index("ix_audit_events_tenant_case", "tenant_id", "case_id"),
        Index("ix_audit_events_tenant_correlation", "tenant_id", "correlation_id"),
    )


# ---------------------------------------------------------------------------
# Signed audit batches (FM-046 / P09-US9 / ADR-009 — Gated)
#
# INTEGRATION_SEAM: Azure SIEM
#
# The rows below are an explicit placeholder for the signed-batch surface
# that auditors request through ``GET /api/v1/audit/signed-batches``. The
# actual schema is deferred until a concrete Azure SIEM target is selected
# (ArcSight / Sentinel / Splunk / LogRhythm — the signature algorithm
# and key-management seam depend on that choice). For the v1 slice we
# expose a typed in-memory implementation behind the application port so
# the use case, the HTTP route, and the tests can converge without a
# dialect-specific table.
#
# The placeholder columns document the **minimum** row shape a production
# adapter must satisfy so the seam is unambiguous. Migrations land with
# the Azure adapter in a follow-up slice; until then no DDL is emitted
# (a SQL adapter cannot reference an undefined table on sqlite /
# postgres).
# ---------------------------------------------------------------------------


class SignedAuditBatch(Base):
    """A tamper-evident batch of audit rows for off-system verification (FM-046).

    The shape mirrors the ``SignedBatch`` dataclass in
    ``app.application.audit.siem_stream`` plus a few durability fields
    (``created_at`` for ordering, ``key_id`` for the rotating signing
    key). The ``payload`` column is the canonical JSON of every row in
    the batch (hashed, not raw) so the signed ``signature`` is
    reproducible off-system. The raw rows are NOT re-serialized here;
    the auditor re-reads them through the existing audit read API to
    verify the chain.

    Why a placeholder table with no real DDL: ADR-009 reserves the
    Azure SIEM target as **Gated** — implementation requires an
    explicit product decision on the SIEM vendor and key-management
    seam before a migration is written. Shipping an empty table would
    commit us to a shape we may have to redesign. The application
    layer reads/writes through a Protocol (see
    ``app.application.audit.siem_stream``); the SQL adapter registers
    itself only after a concrete Azure target is selected.
    """

    __tablename__ = "signed_audit_batches"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # Anchor the batch to the first and last audit row it covers; the
    # auditor uses these to re-read the durable chain and verify the
    # batch against the live ``audit_events`` rows.
    first_event_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    last_event_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    first_event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    last_event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Algorithm + key id are explicit so the auditor knows which key
    # produced the signature (rotation history is required by FM-046
    # acceptance).
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(1024), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)

    __table_args__ = (Index("ix_signed_audit_batches_tenant_created", "tenant_id", "created_at"),)


class Outbox(Base):
    """Durable outbox row (AGENTS.md §12, plan §4.R12, T028).

    Written in the same DB transaction as the domain change. The
    dispatcher drains rows with retry/backoff and dead-letters after N
    attempts.
    """

    __tablename__ = "outbox"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # T029: earliest time the dispatcher may retry this row. NULL means
    # "due immediately" (fresh rows and replays). Set to
    # ``now + backoff(attempt_count)`` on every failure so a poison
    # message does not hot-spin the drain loop; the drain query skips
    # rows whose time has not come (same transaction as the domain
    # change is unaffected — this column only gates retries).
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_outbox_tenant_dispatched", "tenant_id", "dispatched_at"),)


class IdempotencyKey(Base):
    """Durable Idempotency-Key store (AGENTS.md §10, plan §4.R7, T022).

    Scoped per tenant. The first successful response is replayed on
    subsequent requests with the same key AND the same request hash. A
    same key with a different request hash returns 409.
    """

    __tablename__ = "idempotency_keys"

    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # T024: key-expiry timestamp (migration 0006). The middleware does
    # not enforce expiry yet — the column exists so retention tooling
    # can garbage-collect stale keys without a schema change.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_idempotency_keys_tenant", "tenant_id"),)
