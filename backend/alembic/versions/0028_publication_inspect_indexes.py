"""Add FM-021 inspect/approval indexes (portable, reversible).

Adds two B-tree indexes used by the publication-history inspect
endpoint and the second-admin approval gate:

- ``ix_configuration_publications_tenant_version`` on
  ``configuration_publications (tenant_id, version_id)`` — the
  approval gate looks up the publish/validate chain for one version.
- ``ix_case_type_versions_tenant_case_version`` on
  ``case_type_versions (tenant_id, case_type_id, version_number)`` —
  tenant-scoped history/inspect ordering without scanning.

Portable DDL (generic ``create_index``/``drop_index`` only, no
JSONB/GIN/ILIKE/tsvector). Reversible: ``downgrade`` drops both
indexes. Verified on sqlite (``alembic upgrade head`` +
``alembic downgrade -1`` round-trip).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028_publication_inspect_indexes"
down_revision: str | None = "0027_import_mapping_publish_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_configuration_publications_tenant_version",
        "configuration_publications",
        ["tenant_id", "version_id"],
    )
    op.create_index(
        "ix_case_type_versions_tenant_case_version",
        "case_type_versions",
        ["tenant_id", "case_type_id", "version_number"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_case_type_versions_tenant_case_version",
        table_name="case_type_versions",
    )
    op.drop_index(
        "ix_configuration_publications_tenant_version",
        table_name="configuration_publications",
    )
