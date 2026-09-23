"""add document redactions + lock columns (P03-US6, US8 / FM-025)

Adds the ``documents.locked`` / ``locked_at`` / ``locked_by`` columns
so a document can be marked immutable once filed, and the
``document_redactions`` child table that holds versioned redactions
(preserving the original bytes).

# Schema

- ``documents.locked`` (Boolean, NOT NULL, default=False) — when True,
  the document refuses new versions and new redactions
  (``Document.with_new_version`` raises ``DocumentLocked``;
  ``protect_document.redact`` raises ``DocumentLocked``).
- ``documents.locked_at`` (DateTime(timezone=True), nullable) —
  populated by ``Document.with_lock``.
- ``documents.locked_by`` (Uuid, nullable) — populated by
  ``Document.with_lock``.
- ``document_redactions`` — versioned redaction rows. Each row points
  at a ``Document`` and a ``base_version_number``; the redacted bytes
  are content-addressed via ``sha256`` + ``storage_ref``. The original
  ``Document`` + ``DocumentVersion`` rows are NOT modified, so the
  original bytes remain retrievable for audit replay.

# Portability

All column types are SQLAlchemy generic types (Uuid, JSON,
DateTime(timezone=True), Integer, String, Text, Boolean). No
``gen_random_uuid()``, no JSONB, no exclusion constraints. Both
SQLite (tests) and PostgreSQL/Azure SQL accept the shape.

# Reversibility

``downgrade`` drops the indexes + the new columns + the new table in
the reverse order they were added.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_create_document_redactions_and_lock"
down_revision: str | None = "0008_create_import_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. documents.locked / locked_at / locked_by (P03-US8).
    op.add_column(
        "documents",
        sa.Column(
            "locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "locked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "locked_by",
            sa.Uuid(),
            nullable=True,
        ),
    )

    # 2. document_redactions (P03-US6).
    op.create_table(
        "document_redactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_document_redactions_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey(
                "documents.id",
                ondelete="CASCADE",
                name="fk_document_redactions_document_id_documents",
            ),
            nullable=False,
        ),
        sa.Column("base_version_number", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("storage_ref", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=500), nullable=True),
        # status: ACTIVE | SUPERSEDED. Both are kept; SUPERSEDED rows
        # are never deleted so the audit timeline can replay the
        # lineage of redacted bytes.
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "superseded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_document_redactions"),
    )
    op.create_index(
        "ix_document_redactions_tenant_doc",
        "document_redactions",
        ["tenant_id", "document_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_document_redactions_tenant_doc_status",
        "document_redactions",
        ["tenant_id", "document_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_redactions_tenant_doc_status",
        table_name="document_redactions",
    )
    op.drop_index(
        "ix_document_redactions_tenant_doc",
        table_name="document_redactions",
    )
    op.drop_table("document_redactions")
    op.drop_column("documents", "locked_by")
    op.drop_column("documents", "locked_at")
    op.drop_column("documents", "locked")
