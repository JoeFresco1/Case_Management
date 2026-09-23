"""Reconcile document tables with the ORM models (T058).

Migration ``0005_create_workspace_tables`` drifted from
``app/infrastructure/persistence/models.py`` in three places:

- ``document_versions.version`` vs the model's ``version_number``
  (the SQL adapter reads/writes ``version_number`` — on a migrated
  database every document write fails with "no such column").
- ``document_versions`` is missing ``content_type`` (model +
  adapter require it, ``NOT NULL``).
- ``documents.uploaded_at`` exists in the migration but in neither
  the model nor the domain ``Document`` (write-never/read-never
  dead column from an earlier iteration).

This migration renames the version column (data-preserving), adds
``content_type`` (empty-string backfill for pre-release rows; every
writer supplies a real value), drops the dead ``uploaded_at``
column, and aligns the unique constraint + indexes with the model
names.

Portability (AGENTS.md R1): SQLite cannot drop a unique constraint
outside a table rebuild, and a rebuild does not preserve inline
constraint names (they surface as ``sqlite_autoindex_*``). So:

- SQLite renames the column (SQLite rewrites the carried unique
  constraint's column reference automatically — enforcement stays
  on ``(document_id, version_number)`` under the legacy constraint
  name), adds the column, and swaps the named indexes only.
- PostgreSQL additionally renames the unique constraint to the
  model name.

Generic types only, no dialect SQL. Downgrade mirrors both paths
(re-adding ``uploaded_at`` as ``NOT NULL`` fails loudly on tables
that gained rows after the upgrade rather than silently nulling
them).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_reconcile_document_tables"
down_revision: str | None = "0016_scope_idempotency_key_by_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UQ_NEW = "uq_document_versions_doc_version"
_UQ_OLD = "uq_document_versions_document_version"
_IX_NEW = "ix_document_versions_tenant_doc"
_IX_OLD_TENANT = "ix_document_versions_tenant_id"
_IX_OLD_DOC = "ix_document_versions_document_id"


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    if _is_sqlite():
        with op.batch_alter_table("document_versions") as batch:
            batch.alter_column(
                "version",
                new_column_name="version_number",
                existing_type=sa.Integer(),
                existing_nullable=False,
            )
            batch.add_column(
                sa.Column(
                    "content_type",
                    sa.String(length=255),
                    nullable=False,
                    server_default="",
                )
            )
            batch.drop_index(_IX_OLD_TENANT)
            batch.drop_index(_IX_OLD_DOC)
            batch.create_index(_IX_NEW, ["tenant_id", "document_id"])
        with op.batch_alter_table("documents") as batch:
            batch.drop_column("uploaded_at")
    else:
        op.alter_column(
            "document_versions",
            "version",
            new_column_name="version_number",
            existing_type=sa.Integer(),
            existing_nullable=False,
        )
        op.add_column(
            "document_versions",
            sa.Column(
                "content_type",
                sa.String(length=255),
                nullable=False,
                server_default="",
            ),
        )
        op.drop_constraint(_UQ_OLD, "document_versions", type_="unique")
        op.create_unique_constraint(_UQ_NEW, "document_versions", ["document_id", "version_number"])
        op.drop_index(_IX_OLD_TENANT, table_name="document_versions")
        op.drop_index(_IX_OLD_DOC, table_name="document_versions")
        op.create_index(_IX_NEW, "document_versions", ["tenant_id", "document_id"])
        op.drop_column("documents", "uploaded_at")


def downgrade() -> None:
    if _is_sqlite():
        with op.batch_alter_table("documents") as batch:
            batch.add_column(
                sa.Column(
                    "uploaded_at",
                    sa.DateTime(timezone=True),
                    nullable=False,
                )
            )
        with op.batch_alter_table("document_versions") as batch:
            batch.drop_index(_IX_NEW)
            batch.create_index(_IX_OLD_DOC, ["document_id"], unique=False)
            batch.create_index(_IX_OLD_TENANT, ["tenant_id"], unique=False)
            batch.drop_column("content_type")
            batch.alter_column(
                "version_number",
                new_column_name="version",
                existing_type=sa.Integer(),
                existing_nullable=False,
            )
    else:
        op.add_column(
            "documents",
            sa.Column(
                "uploaded_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
        )
        op.drop_index(_IX_NEW, table_name="document_versions")
        op.create_index(_IX_OLD_DOC, "document_versions", ["document_id"], unique=False)
        op.create_index(_IX_OLD_TENANT, "document_versions", ["tenant_id"], unique=False)
        op.drop_constraint(_UQ_NEW, "document_versions", type_="unique")
        op.create_unique_constraint(_UQ_OLD, "document_versions", ["document_id", "version_number"])
        op.drop_column("document_versions", "content_type")
        op.alter_column(
            "document_versions",
            "version_number",
            new_column_name="version",
            existing_type=sa.Integer(),
            existing_nullable=False,
        )
