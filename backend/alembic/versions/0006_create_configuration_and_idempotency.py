"""create configuration_publications and idempotency_keys tables

Foundation hardening tables (T024 / T025 / plan §4.R10, AGENTS.md §16.1).

# Schema

- ``configuration_publications``: append-only audit trail of every
  configuration lifecycle action (draft → validate → publish → retire).
  One row per action; ``change_summary`` is the structured human-readable
  note. The companion ``configuration_publications`` view (T033+
  timeline + admin endpoints) is the read path.
- ``idempotency_keys``: durable Idempotency-Key store (T022). Scoped
  per tenant; the first successful response is replayed on
  subsequent requests with the same key AND the same request hash. A
  same key with a different request hash returns 409.
  ``correlation_id`` ties the entry to the request log line so
  duplicate requests are observable in telemetry.

# Portability

All column types are SQLAlchemy generic types. Foreign keys cascade
on tenant delete; artifact FKs are nullable because the artifact
itself may be retired independently of the publication record.

# Reversibility

``downgrade`` drops indexes, then tables in reverse dependency
order (configuration_publications -> idempotency_keys).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_create_configuration_and_idempotency"
down_revision: str | None = "0005_create_workspace_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- configuration_publications --------------------------------------
    # Why append-only: this table is the durable record of who did
    # what to which workflow/case-type, when, and why. Corrections
    # append a new row; there is no UPDATE/DELETE at any layer.
    #
    # Why artifact_id / version_id are nullable+no FK: the artifact
    # may be retired (status='retired') or even deleted later (the
    # publication history outlives the artifact); the FK would
    # either need to chain ON DELETE CASCADE (losing history) or
    # ON DELETE RESTRICT (blocking legitimate cleanup). The
    # application tier resolves the artifact on read; the table
    # itself only enforces the tenant boundary.
    op.create_table(
        "configuration_publications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_configuration_publications_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("version_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # Why Text: change_summary is human-authored and may be long;
        # the column is scan-friendly on sqlite and PostgreSQL.
        sa.Column("change_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_configuration_publications"),
    )
    op.create_index(
        "ix_configuration_publications_tenant_id",
        "configuration_publications",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_configuration_publications_artifact",
        "configuration_publications",
        ["tenant_id", "artifact_type", "artifact_id"],
        unique=False,
    )

    # ---- idempotency_keys -------------------------------------------------
    # Why primary key is the (tenant_id, key) composite: two tenants
    # may legitimately use the same client-generated key. The
    # composite PK is portable on both sqlite and PostgreSQL — both
    # dialects support multi-column primary keys without any
    # dialect-specific options.
    #
    # Why request_hash: idempotency requires replaying the response
    # for the same logical operation. A different request body under
    # the same key signals a client bug and must return 409 (T022).
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_idempotency_keys_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key", "tenant_id", name="pk_idempotency_keys"),
    )
    op.create_index(
        "ix_idempotency_keys_tenant_id",
        "idempotency_keys",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_idempotency_keys_expires_at",
        "idempotency_keys",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_index("ix_idempotency_keys_tenant_id", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")

    op.drop_index(
        "ix_configuration_publications_artifact",
        table_name="configuration_publications",
    )
    op.drop_index(
        "ix_configuration_publications_tenant_id",
        table_name="configuration_publications",
    )
    op.drop_table("configuration_publications")
