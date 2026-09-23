"""Versioned forms, bindings, and multi-step sessions (FM-036/FM-038).

One row per ``(case, form version, actor)`` attempt carrying the
worker's durable progress: current step index, completed steps, and
accumulated partial payload. Status lifecycle is
``draft`` → ``in_progress`` → ``submitted`` (terminal); submitted rows
are never updated (reopening is a new row).

The form-version and binding tables are created here before sessions so
their foreign keys are valid on both PostgreSQL and SQLite.  Form definitions
are immutable JSON snapshots on ``form_versions``; the stable ``key`` and
``version_number`` identify their lineage.

Portable DDL (generic types only). Reversible in dependency order.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_form_sessions"
down_revision: str | None = "0023_break_glass_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "form_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_form_versions_tenant_id_tenants",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_form_versions"),
        sa.UniqueConstraint(
            "tenant_id",
            "key",
            "version_number",
            name="uq_form_versions_tenant_key_version",
        ),
    )
    op.create_index("ix_form_versions_tenant_status", "form_versions", ["tenant_id", "status"])
    op.create_index("ix_form_versions_tenant_key", "form_versions", ["tenant_id", "key"])

    op.create_table(
        "form_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_type_version_id", sa.Uuid(), nullable=False),
        sa.Column("form_version_id", sa.Uuid(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state_forms", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_form_bindings_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["case_type_version_id"],
            ["case_type_versions.id"],
            ondelete="RESTRICT",
            name="fk_form_bindings_case_type_version_id_case_type_versions",
        ),
        sa.ForeignKeyConstraint(
            ["form_version_id"],
            ["form_versions.id"],
            ondelete="RESTRICT",
            name="fk_form_bindings_form_version_id_form_versions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_form_bindings"),
        sa.UniqueConstraint(
            "tenant_id",
            "case_type_version_id",
            "form_version_id",
            name="uq_form_bindings_tenant_ctv_fv",
        ),
    )
    op.create_index(
        "ix_form_bindings_tenant_ctv",
        "form_bindings",
        ["tenant_id", "case_type_version_id"],
    )
    op.create_index(
        "ix_form_bindings_tenant_fv",
        "form_bindings",
        ["tenant_id", "form_version_id"],
    )
    op.create_index(
        "ix_form_bindings_tenant_published",
        "form_bindings",
        ["tenant_id", "published_at"],
    )

    op.create_table(
        "form_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("form_version_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("current_step_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "completed_steps",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "step_data",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="in_progress"),
        sa.Column("version_token", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="CASCADE",
            name="fk_form_sessions_tenant_id_tenants",
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
            ondelete="RESTRICT",
            name="fk_form_sessions_case_id_cases",
        ),
        sa.ForeignKeyConstraint(
            ["form_version_id"],
            ["form_versions.id"],
            ondelete="RESTRICT",
            name="fk_form_sessions_form_version_id_form_versions",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_form_sessions"),
    )
    op.create_index("ix_form_sessions_tenant_case", "form_sessions", ["tenant_id", "case_id"])
    op.create_index(
        "ix_form_sessions_open_lookup",
        "form_sessions",
        ["tenant_id", "case_id", "form_version_id", "actor_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_form_sessions_open_lookup", table_name="form_sessions")
    op.drop_index("ix_form_sessions_tenant_case", table_name="form_sessions")
    op.drop_table("form_sessions")
    op.drop_index("ix_form_bindings_tenant_published", table_name="form_bindings")
    op.drop_index("ix_form_bindings_tenant_fv", table_name="form_bindings")
    op.drop_index("ix_form_bindings_tenant_ctv", table_name="form_bindings")
    op.drop_table("form_bindings")
    op.drop_index("ix_form_versions_tenant_key", table_name="form_versions")
    op.drop_index("ix_form_versions_tenant_status", table_name="form_versions")
    op.drop_table("form_versions")
