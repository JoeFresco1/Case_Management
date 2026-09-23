"""Reconcile workflow runtime columns used by the application."""

import sqlalchemy as sa

from alembic import op

revision = "0015_reconcile_workflow_runtime"
down_revision = "0014_create_queues"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These columns pin the owning definition and provide optimistic locking.
    op.add_column(
        "workflow_instances", sa.Column("workflow_definition_id", sa.Uuid(), nullable=True)
    )
    op.add_column("workflow_instances", sa.Column("version_token", sa.Integer(), nullable=True))
    op.execute("UPDATE workflow_instances SET version_token = 1")
    op.alter_column("workflow_instances", "workflow_definition_id", nullable=False)
    op.alter_column("workflow_instances", "version_token", nullable=False)


def downgrade() -> None:
    op.drop_column("workflow_instances", "version_token")
    op.drop_column("workflow_instances", "workflow_definition_id")
