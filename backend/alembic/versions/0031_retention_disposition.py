"""Retention + disposition durable tables (FM-040 / P08-US3, US4).

F-2/F-3 follow-up: ``models.py`` already declares ``RetentionPolicy``
(versioned retention declaration) and ``DispositionRequest`` (per-case
review queue), and the domain + in-memory adapters + routers are wired —
but the migration chain never created either table, so ``alembic check``
drifts and the ORM round-trip fails on migrated DDL. This revision
creates both missing tables in one step.

Scope notes (why only two tables here):

- Holds (``legal_holds``) already landed in 0026 (FM-039) — not repeated.
- Archival / freeze / orphan rows (FM-041) remain in-memory seam
  placeholders by design; no ORM model declares them yet, so no DDL is
  emitted for them here.
- Evidence bundles (FM-060) are computed ZIP archives, not rows; the
  export is itself an ``AuditExported`` row on the existing chain.
- Report definitions (FM-053) remain in-memory (``ReportDefinition``
  repository); no ORM model declares them yet, so no DDL here.

Portable DDL (generic types only — AGENTS.md R1). No JSONB, no GIN,
no ILIKE, no tsvector, no gen_random_uuid(). Reversible:
``downgrade`` drops indexes then tables in reverse creation order.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_retention_disposition"
down_revision: str | None = "0030_capture_quarantine_webhook"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retention_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("target_key", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_retention_policies_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_retention_policies"),
        sa.UniqueConstraint(
            "tenant_id",
            "category",
            "target_key",
            "version_number",
            name="uq_retention_policies_version",
        ),
    )
    op.create_index(
        "ix_retention_policies_tenant_target",
        "retention_policies",
        ["tenant_id", "category", "target_key"],
    )
    op.create_index(
        "ix_retention_policies_tenant_status",
        "retention_policies",
        ["tenant_id", "status"],
    )

    op.create_table(
        "disposition_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("surfaced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("surfaced_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_disposition_requests_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
            ondelete="CASCADE",
            name="fk_disposition_requests_case_id_cases",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["retention_policies.id"],
            ondelete="RESTRICT",
            name="fk_disposition_requests_policy_id_retention_policies",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_disposition_requests"),
    )
    op.create_index(
        "ix_disposition_requests_tenant_case_status",
        "disposition_requests",
        ["tenant_id", "case_id", "status"],
    )
    op.create_index(
        "ix_disposition_requests_tenant_status",
        "disposition_requests",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_disposition_requests_tenant_status", table_name="disposition_requests")
    op.drop_index("ix_disposition_requests_tenant_case_status", table_name="disposition_requests")
    op.drop_table("disposition_requests")
    op.drop_index("ix_retention_policies_tenant_status", table_name="retention_policies")
    op.drop_index("ix_retention_policies_tenant_target", table_name="retention_policies")
    op.drop_table("retention_policies")
