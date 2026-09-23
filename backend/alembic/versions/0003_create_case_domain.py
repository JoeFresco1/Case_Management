"""create case_types, case_type_versions, and cases tables

Walking-skeleton case domain tables (T011 / plan §4.R10, AGENTS.md §16.1).

# Schema

- ``case_types``: tenant-scoped configuration catalog of case types.
  Each tenant has at most one row per ``key`` (``uq_case_types_tenant_key``).
  ``current_version_id`` is denormalized for fast read; it is updated
  by the publish path (T073) and is nullable for case types that have
  never been published.
- ``case_type_versions``: immutable versioned schemas. Each row pins a
  ``schema_json`` blob plus denormalized ``document_classifications``
  and ``rules`` arrays. Once ``status='published'`` any UPDATE/DELETE
  is rejected at the application layer (``case_type.py::publish``).
- ``cases``: the case aggregate. The reference is monotonic per tenant
  (``C-{TENANT}-{YYYY}-{SEQ:06d}``) and is unique per tenant
  (``uq_cases_tenant_ref``). ``version_token`` enables optimistic
  concurrency on ``If-Match`` (``case.py::edit``, T043/T044).

# Portability (AGENTS.md §4.R1, plan §3)

All column types are SQLAlchemy generic types (``Uuid``, ``JSON``,
``DateTime(timezone=True)``, ``Text``, ``String``, ``Integer``). No
``JSONB``, no dialect-specific UUID syntax. Identifier defaults are
application-side ``uuid4()``; the migration never calls
``gen_random_uuid()``. Indexes are plain B-tree ``ix_*``. Foreign keys
are tenant-scoped and cascade on tenant delete.

# Reversibility

``downgrade`` drops indexes, then tables in reverse dependency order.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_create_case_domain"
down_revision: str | None = "0002_create_audit_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- case_types -------------------------------------------------------
    # Why: case_types is the configuration catalog for case schemas
    # (P05). Each tenant owns its own catalog; uniqueness is enforced
    # at (tenant_id, key) — a global unique constraint would block
    # tenants from adopting the same short key (e.g., "ServiceRequest").
    op.create_table(
        "case_types",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE", name="fk_case_types_tenant_id_tenants"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        # Why Text (not String): descriptions can include rich markup;
        # the 5000-char ceiling keeps the column scan-friendly on both
        # sqlite and PostgreSQL/Azure SQL.
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "sensitivity_default",
            sa.String(length=32),
            nullable=False,
            server_default="internal",
        ),
        # Why nullable: a brand-new case type has no published version
        # yet. The publish path (T073) sets this column to the new
        # version's id in the same transaction.
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_case_types"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_case_types_tenant_key"),
    )
    op.create_index("ix_case_types_tenant_id", "case_types", ["tenant_id"], unique=False)

    # ---- case_type_versions ----------------------------------------------
    # Why immutable on publish: AGENTS.md §4.R12 / plan §4.R12 — once
    # status='published' the row is frozen. Application code enforces
    # the rule (case_type.py::publish raises on UPDATE to published);
    # the migration itself never mutates status back to draft.
    op.create_table(
        "case_type_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_case_type_versions_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column(
            "case_type_id",
            sa.Uuid(),
            sa.ForeignKey(
                "case_types.id",
                ondelete="CASCADE",
                name="fk_case_type_versions_case_type_id_case_types",
            ),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        # Why JSON (generic) and not JSONB: portability per R1. The
        # schema blob is small (field list + rules) and the publish
        # history reader parses it in Python; no portable GIN index is
        # required.
        sa.Column("schema_json", sa.JSON(), nullable=False),
        # Why denormalized arrays: timeline/history reads (P10-US4)
        # answer "what classifications were allowed on this version"
        # without re-parsing schema_json.
        sa.Column("document_classifications", sa.JSON(), nullable=False),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_case_type_versions"),
        sa.UniqueConstraint(
            "case_type_id",
            "version_number",
            name="uq_case_type_versions_case_type_version",
        ),
    )
    op.create_index(
        "ix_case_type_versions_tenant_id",
        "case_type_versions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_case_type_versions_case_type_status",
        "case_type_versions",
        ["case_type_id", "status"],
        unique=False,
    )

    # ---- cases ------------------------------------------------------------
    # Why RESTRICT on case_type_version_id: deleting a published
    # version that is still in use would corrupt the case's data
    # contract. Publish flow retires old versions (status='retired')
    # rather than deleting them; the FK guarantees no orphan case can
    # outlive its schema reference.
    op.create_table(
        "cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE", name="fk_cases_tenant_id_tenants"),
            nullable=False,
        ),
        # Why CASCADE on case_type_version_id would be wrong: a
        # retired version must remain addressable by historical cases.
        # RESTRICT forces the application to retire, not delete.
        sa.Column(
            "case_type_version_id",
            sa.Uuid(),
            sa.ForeignKey(
                "case_type_versions.id",
                ondelete="RESTRICT",
                name="fk_cases_case_type_version_id_case_type_versions",
            ),
            nullable=False,
        ),
        sa.Column("reference", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("current_workflow_state", sa.String(length=64), nullable=True),
        sa.Column(
            "version_token",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_cases"),
        sa.UniqueConstraint("tenant_id", "reference", name="uq_cases_tenant_reference"),
    )
    op.create_index("ix_cases_tenant_id", "cases", ["tenant_id"], unique=False)
    op.create_index(
        "ix_cases_tenant_status",
        "cases",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_cases_case_type_version_id",
        "cases",
        ["case_type_version_id"],
        unique=False,
    )


def downgrade() -> None:
    # Reverse dependency order: cases depends on case_type_versions
    # depends on case_types depends on tenants.
    op.drop_index("ix_cases_case_type_version_id", table_name="cases")
    op.drop_index("ix_cases_tenant_status", table_name="cases")
    op.drop_index("ix_cases_tenant_id", table_name="cases")
    op.drop_table("cases")

    op.drop_index("ix_case_type_versions_case_type_status", table_name="case_type_versions")
    op.drop_index("ix_case_type_versions_tenant_id", table_name="case_type_versions")
    op.drop_table("case_type_versions")

    op.drop_index("ix_case_types_tenant_id", table_name="case_types")
    op.drop_table("case_types")
