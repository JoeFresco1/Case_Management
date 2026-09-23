"""create workflow_definitions, workflow_versions, workflow_instances, workflow_transitions

Workflow engine tables (T069 / plan §4.R10, AGENTS.md §16.1, P02, P10).

# Schema

- ``workflow_definitions``: tenant-scoped catalog of workflow templates
  keyed by ``(tenant_id, key)``. ``current_version_id`` is the
  published version's id; nullable for never-published workflows.
- ``workflow_versions``: immutable versioned definitions. The
  ``definition_json`` holds states, transitions, guards, and actions.
  Once ``status='published'`` any UPDATE/DELETE is rejected at the
  application layer (workflow.py::publish).
- ``workflow_instances``: per-case runtime. ``current_state`` is the
  state machine pointer; ``correlation_id`` ties the instance to the
  originating case-creation transaction. One instance per case
  (uq_workflow_instances_case_id) — enforced because the workflow
  always starts on case creation (T070) and never restarted.
- ``workflow_transitions``: append-only history. Each row records the
  (from_state, to_state, transition_key, actor_id, payload) tuple and
  is the audit-evidence source for what actually happened.

# Portability

All column types are SQLAlchemy generic types (``Uuid``, ``JSON``,
``DateTime(timezone=True)``, ``Text``, ``String``, ``Integer``).
Indexes are plain B-tree ``ix_*``. Foreign keys cascade on tenant
delete; case-related FKs use ``RESTRICT`` to protect history.

# Reversibility

``downgrade`` drops indexes, then tables in reverse dependency order
(workflow_transitions -> workflow_instances -> workflow_versions ->
workflow_definitions).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_create_workflow_domain"
down_revision: str | None = "0003_create_case_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- workflow_definitions --------------------------------------------
    op.create_table(
        "workflow_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_workflow_definitions_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        # Why nullable: a brand-new workflow has no published version.
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
        sa.PrimaryKeyConstraint("id", name="pk_workflow_definitions"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_workflow_definitions_tenant_key"),
    )
    op.create_index(
        "ix_workflow_definitions_tenant_id",
        "workflow_definitions",
        ["tenant_id"],
        unique=False,
    )

    # ---- workflow_versions -----------------------------------------------
    # Why immutable on publish: AGENTS.md §4.R12 — once status='published'
    # the row is frozen. Application code enforces the rule
    # (workflow.py::publish raises on UPDATE to published); the
    # migration never mutates status back to draft.
    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_workflow_versions_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column(
            "workflow_definition_id",
            sa.Uuid(),
            sa.ForeignKey(
                "workflow_definitions.id",
                ondelete="CASCADE",
                name="fk_workflow_versions_workflow_definition_id_workflow_definitions",
            ),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        # Why JSON (not JSONB): portability per R1. The definition blob
        # holds states, transitions, guards, and actions; no GIN
        # indexing is needed at this slice (validation in T072 is a
        # pure Python pass).
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_versions"),
        sa.UniqueConstraint(
            "workflow_definition_id",
            "version_number",
            name="uq_workflow_versions_definition_version",
        ),
    )
    op.create_index(
        "ix_workflow_versions_tenant_id",
        "workflow_versions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_versions_definition_status",
        "workflow_versions",
        ["workflow_definition_id", "status"],
        unique=False,
    )

    # ---- workflow_instances ----------------------------------------------
    # Why unique on case_id: one workflow per case (T070 starts it at
    # case creation; subsequent case edits do not restart). A second
    # row would imply two competing state machines.
    op.create_table(
        "workflow_instances",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_workflow_instances_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            sa.Uuid(),
            sa.ForeignKey(
                "cases.id",
                ondelete="RESTRICT",
                name="fk_workflow_instances_case_id_cases",
            ),
            nullable=False,
        ),
        sa.Column(
            "workflow_version_id",
            sa.Uuid(),
            sa.ForeignKey(
                "workflow_versions.id",
                ondelete="RESTRICT",
                name="fk_workflow_instances_workflow_version_id_workflow_versions",
            ),
            nullable=False,
        ),
        sa.Column("current_state", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_workflow_instances"),
        sa.UniqueConstraint("case_id", name="uq_workflow_instances_case_id"),
    )
    op.create_index(
        "ix_workflow_instances_tenant_id",
        "workflow_instances",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_instances_workflow_version_id",
        "workflow_instances",
        ["workflow_version_id"],
        unique=False,
    )

    # ---- workflow_transitions --------------------------------------------
    # Why append-only: the transition log IS the evidence of what
    # happened to a case. Corrections are written as compensating
    # ``WorkflowTransitioned`` rows that reference the original; there
    # is no UPDATE/DELETE on this table at any layer.
    op.create_table(
        "workflow_transitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_workflow_transitions_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column(
            "workflow_instance_id",
            sa.Uuid(),
            sa.ForeignKey(
                "workflow_instances.id",
                ondelete="CASCADE",
                name="fk_workflow_transitions_workflow_instance_id_workflow_instances",
            ),
            nullable=False,
        ),
        sa.Column("from_state", sa.String(length=64), nullable=False),
        sa.Column("to_state", sa.String(length=64), nullable=False),
        sa.Column("transition_key", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # Why JSON (not JSONB): payload is the structured input to the
        # transition's actions (e.g., decision rationale, document
        # refs). Portable across sqlite/PostgreSQL/Azure SQL.
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("causation_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_transitions"),
    )
    op.create_index(
        "ix_workflow_transitions_tenant_id",
        "workflow_transitions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_transitions_instance_occurred",
        "workflow_transitions",
        ["workflow_instance_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_transitions_instance_occurred",
        table_name="workflow_transitions",
    )
    op.drop_index("ix_workflow_transitions_tenant_id", table_name="workflow_transitions")
    op.drop_table("workflow_transitions")

    op.drop_index(
        "ix_workflow_instances_workflow_version_id",
        table_name="workflow_instances",
    )
    op.drop_index("ix_workflow_instances_tenant_id", table_name="workflow_instances")
    op.drop_table("workflow_instances")

    op.drop_index("ix_workflow_versions_definition_status", table_name="workflow_versions")
    op.drop_index("ix_workflow_versions_tenant_id", table_name="workflow_versions")
    op.drop_table("workflow_versions")

    op.drop_index("ix_workflow_definitions_tenant_id", table_name="workflow_definitions")
    op.drop_table("workflow_definitions")
