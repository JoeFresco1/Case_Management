"""create import_jobs table

Foundation for P14-US2 / US3 / US4 and P04-US3 — bulk ingestion from
an approved source via a versioned mapping. The first slice ships
with synthetic fixtures only (AGENTS.md §18); live enterprise data
imports are gated behind explicit connector authorization (P14 §2 /
Constitution V.10).

# Schema

``import_jobs`` carries:

- identity + bookkeeping: ``id``, ``tenant_id``, ``source_label``,
  ``source_format``, ``source_row_count``, ``status``,
  ``correlation_id``, ``created_by``, ``created_at``, ``updated_at``,
  ``committed_at``, ``failure_reason``.
- a snapshot of the mapping (``mapping_json``) so a re-run uses the
  mapping that was active when the job was committed (Constitution XI
  — incremental migration; future versions of the mapping cannot
  silently change the meaning of a historical import).
- a per-row reconciliation (``reconciliation_json``) and per-row
  results (``results_json``) so a follow-up read does not have to
  recompute the report.

# Portability

All column types are SQLAlchemy generic types (Uuid, JSON,
DateTime(timezone=True), Integer, String, Text). No ``gen_random_uuid()``,
no JSONB, no exclusion constraints. Both SQLite (tests) and
PostgreSQL/Azure SQL accept the shape.

# Reversibility

``downgrade`` drops the indexes, then the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_create_import_jobs"
down_revision: str | None = "0007_create_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_import_jobs_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column("source_label", sa.String(length=200), nullable=False),
        sa.Column("source_format", sa.String(length=16), nullable=False),
        sa.Column("source_row_count", sa.Integer(), nullable=False, server_default="0"),
        # Why JSON: the mapping document is versioned and the application
        # layer is the canonical schema. Storing the raw JSON snapshot
        # means a future version bump cannot silently change the
        # semantics of a historical import (Constitution XI).
        sa.Column("mapping_json", sa.JSON(), nullable=False),
        # status: pending | previewed | committing | committed | failed
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "committed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("reconciliation_json", sa.JSON(), nullable=True),
        # Why default=list: the column is non-null but the empty
        # results payload is a valid initial state. Using a callable
        # default keeps the JSON column portable (no SQL DEFAULT in the
        # database).
        sa.Column(
            "results_json",
            sa.JSON(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_jobs"),
    )
    op.create_index(
        "ix_import_jobs_tenant_status",
        "import_jobs",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_import_jobs_tenant_created",
        "import_jobs",
        ["tenant_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_import_jobs_tenant_created", table_name="import_jobs")
    op.drop_index("ix_import_jobs_tenant_status", table_name="import_jobs")
    op.drop_table("import_jobs")
