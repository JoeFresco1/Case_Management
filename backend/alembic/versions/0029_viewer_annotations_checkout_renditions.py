"""Viewer pipeline tables (T5 / FM-023 ext): annotations, checkout locks, renditions.

- ``viewer_annotations`` — the annotation overlay layer (highlight /
  text-box / stamp / freehand) pinned to ``(document_id,
  document_version, page)``. Overlay rows never mutate document bytes.
- ``document_checkout_locks`` — persistent exclusive checkouts with
  holder + expiry; history is the append-only row sequence.
- ``document_renditions`` — burned viewer renditions (redacted burns)
  with content addressing back to the source version.

Portable DDL (generic types only - AGENTS.md R1). No JSONB, no
gen_random_uuid(). Reversible: ``downgrade`` drops indexes then tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_viewer_annotations_checkout_renditions"
down_revision: str | None = "0028_publication_inspect_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "viewer_annotations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("geometry", sa.JSON(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_viewer_annotations_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
            name="fk_viewer_annotations_document_id_documents",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_viewer_annotations"),
    )
    op.create_index(
        "ix_viewer_annotations_tenant_doc",
        "viewer_annotations",
        ["tenant_id", "document_id"],
    )
    op.create_index(
        "ix_viewer_annotations_tenant_doc_version",
        "viewer_annotations",
        ["tenant_id", "document_id", "document_version"],
    )

    op.create_table(
        "document_checkout_locks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("holder_user_id", sa.Uuid(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_checkout_locks_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
            name="fk_checkout_locks_document_id_documents",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_checkout_locks"),
    )
    op.create_index(
        "ix_checkout_locks_tenant_doc",
        "document_checkout_locks",
        ["tenant_id", "document_id"],
    )
    op.create_index(
        "ix_checkout_locks_tenant_doc_released",
        "document_checkout_locks",
        ["tenant_id", "document_id", "released_at"],
    )

    op.create_table(
        "document_renditions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("storage_ref", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("redaction_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_document_renditions_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            ondelete="CASCADE",
            name="fk_document_renditions_document_id_documents",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_renditions"),
    )
    op.create_index(
        "ix_document_renditions_tenant_doc",
        "document_renditions",
        ["tenant_id", "document_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_renditions_tenant_doc", table_name="document_renditions")
    op.drop_table("document_renditions")
    op.drop_index("ix_checkout_locks_tenant_doc_released", table_name="document_checkout_locks")
    op.drop_index("ix_checkout_locks_tenant_doc", table_name="document_checkout_locks")
    op.drop_table("document_checkout_locks")
    op.drop_index("ix_viewer_annotations_tenant_doc_version", table_name="viewer_annotations")
    op.drop_index("ix_viewer_annotations_tenant_doc", table_name="viewer_annotations")
    op.drop_table("viewer_annotations")
