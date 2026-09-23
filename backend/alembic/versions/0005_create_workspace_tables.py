"""create tasks, documents, document_versions, participants, decisions tables

Phase 2 / Phase 4 working tables (T058 / T063 / plan §4.R10,
AGENTS.md §16.1).

# Schema

- ``tasks``: per-case work items with an optional user/queue assignee.
  ``idempotency_key`` is a tenant-scoped unique key that powers
  T059/T061 idempotent completion: a second POST with the same key
  replays the original outcome instead of double-execution.
- ``documents`` + ``document_versions``: the document is the logical
  artifact (sha256, classification); ``document_versions`` is the
  append-only version history. The current pointer
  (``documents.current_version``) is a non-null reference into
  ``document_versions.id`` (invariant at the application layer —
  enforced by ``Document`` aggregate, T053).
- ``participants``: people attached to a case. Removal is logical
  (``removed_at``) so historical references are preserved (T047).
- ``decisions``: case-scoped decision records. ``supersedes_id``
  chains revisions; ``applicable_workflow_version_id`` pins the
  workflow version the decision was made under, so historical
  decisions remain interpretable even after the workflow is updated.

# Portability

All column types are SQLAlchemy generic types. Foreign keys cascade
on tenant delete; case/document FKs use ``RESTRICT`` so the audit
chain stays intact (deleting a case with decisions would orphan the
record, which must be impossible at the DB layer).

# Reversibility

``downgrade`` drops indexes, then tables in reverse dependency
order: ``document_versions`` -> ``documents`` -> ``tasks`` ->
``participants`` -> ``decisions``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_create_workspace_tables"
down_revision: str | None = "0004_create_workflow_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- tasks ------------------------------------------------------------
    # Why idempotency_key is per-tenant unique (not global): two
    # tenants may legitimately use the same client-generated key for
    # separate operations. The idempotency middleware (T022) scopes
    # the replay to the tenant.
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_tasks_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            sa.Uuid(),
            sa.ForeignKey(
                "cases.id",
                ondelete="RESTRICT",
                name="fk_tasks_case_id_cases",
            ),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("assignee_user_id", sa.Uuid(), nullable=True),
        # Queue keys are stable business identifiers (not queue row ids).
        sa.Column("assignee_queue_id", sa.String(length=64), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", sa.Uuid(), nullable=True),
        sa.Column("completion_outcome", sa.JSON(), nullable=True),
        sa.Column("version_token", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by_transition", sa.String(length=128), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_tasks_tenant_idempotency_key",
        ),
    )
    op.create_index("ix_tasks_tenant_id", "tasks", ["tenant_id"], unique=False)
    op.create_index("ix_tasks_case_id", "tasks", ["case_id"], unique=False)
    op.create_index(
        "ix_tasks_tenant_status",
        "tasks",
        ["tenant_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_tasks_tenant_assignee",
        "tasks",
        ["tenant_id", "assignee_user_id"],
        unique=False,
    )

    # ---- documents --------------------------------------------------------
    # Why sha256 is per-tenant unique: content-addressable lookup
    # within a tenant. Two tenants may legitimately share a hash on
    # identical content but keep their document namespaces separate.
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_documents_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            sa.Uuid(),
            sa.ForeignKey(
                "cases.id",
                ondelete="RESTRICT",
                name="fk_documents_case_id_cases",
            ),
            nullable=False,
        ),
        sa.Column("file_name", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("classification", sa.String(length=64), nullable=False),
        sa.Column("sensitivity", sa.String(length=32), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        # Why Text (not String): storage_ref is a backend-specific
        # handle that may include path separators and hash prefixes;
        # it can grow with adapter-specific schemes.
        sa.Column("storage_ref", sa.Text(), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint("tenant_id", "sha256", name="uq_documents_tenant_sha256"),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"], unique=False)
    op.create_index("ix_documents_case_id", "documents", ["case_id"], unique=False)

    # ---- document_versions ------------------------------------------------
    # Append-only version history. The current pointer in
    # ``documents.current_version`` is the version number, and there
    # must always be a matching row here (invariant at app layer).
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_document_versions_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey(
                "documents.id",
                ondelete="CASCADE",
                name="fk_document_versions_document_id_documents",
            ),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("storage_ref", sa.Text(), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.UniqueConstraint("document_id", "version", name="uq_document_versions_document_version"),
    )
    op.create_index(
        "ix_document_versions_tenant_id",
        "document_versions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_document_versions_document_id",
        "document_versions",
        ["document_id"],
        unique=False,
    )

    # ---- participants -----------------------------------------------------
    # Why no unique constraint on (case_id, role, is_primary): a case
    # may have multiple participants in the same role (e.g., two
    # reviewers). The unique identifier is the row id.
    op.create_table(
        "participants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_participants_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            sa.Uuid(),
            sa.ForeignKey(
                "cases.id",
                ondelete="RESTRICT",
                name="fk_participants_case_id_cases",
            ),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        # Why Text: external_ref may include arbitrary external
        # identifiers (LDAP DN, HRIS id, email address).
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_participants"),
    )
    op.create_index("ix_participants_tenant_id", "participants", ["tenant_id"], unique=False)
    op.create_index("ix_participants_case_id", "participants", ["case_id"], unique=False)

    # ---- decisions --------------------------------------------------------
    # Why supersedes_id self-references decisions: decisions can be
    # revised; the revision chain (id -> supersedes_id -> ...) is the
    # append-only history. The chain is read by the timeline endpoint
    # (T034). No UPDATE/DELETE at any layer.
    op.create_table(
        "decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_decisions_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column(
            "case_id",
            sa.Uuid(),
            sa.ForeignKey(
                "cases.id",
                ondelete="RESTRICT",
                name="fk_decisions_case_id_cases",
            ),
            nullable=False,
        ),
        sa.Column("decision_type", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        # Why JSON (not JSONB): evidence payloads (reference ids,
        # doc ids) are small and parsed in Python. Portable per R1.
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("made_by", sa.Uuid(), nullable=False),
        sa.Column("made_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "applicable_workflow_version_id",
            sa.Uuid(),
            sa.ForeignKey(
                "workflow_versions.id",
                ondelete="RESTRICT",
                name="fk_decisions_applicable_workflow_version_id_workflow_versions",
            ),
            nullable=True,
        ),
        sa.Column(
            "supersedes_id",
            sa.Uuid(),
            sa.ForeignKey(
                "decisions.id",
                ondelete="RESTRICT",
                name="fk_decisions_supersedes_id_decisions",
            ),
            nullable=True,
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_decisions"),
    )
    op.create_index("ix_decisions_tenant_id", "decisions", ["tenant_id"], unique=False)
    op.create_index("ix_decisions_case_id", "decisions", ["case_id"], unique=False)
    op.create_index("ix_decisions_supersedes_id", "decisions", ["supersedes_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_decisions_supersedes_id", table_name="decisions")
    op.drop_index("ix_decisions_case_id", table_name="decisions")
    op.drop_index("ix_decisions_tenant_id", table_name="decisions")
    op.drop_table("decisions")

    op.drop_index("ix_participants_case_id", table_name="participants")
    op.drop_index("ix_participants_tenant_id", table_name="participants")
    op.drop_table("participants")

    op.drop_index("ix_document_versions_document_id", table_name="document_versions")
    op.drop_index("ix_document_versions_tenant_id", table_name="document_versions")
    op.drop_table("document_versions")

    op.drop_index("ix_documents_case_id", table_name="documents")
    op.drop_index("ix_documents_tenant_id", table_name="documents")
    op.drop_table("documents")

    op.drop_index("ix_tasks_tenant_assignee", table_name="tasks")
    op.drop_index("ix_tasks_tenant_status", table_name="tasks")
    op.drop_index("ix_tasks_case_id", table_name="tasks")
    op.drop_index("ix_tasks_tenant_id", table_name="tasks")
    op.drop_table("tasks")
