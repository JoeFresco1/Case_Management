"""Scope idempotency keys by tenant.

The original table contract permits the same client-generated key in
different tenants. The migration preserves every row while changing the
primary key from ``key`` to ``(tenant_id, key)``.

The downgrade is intentionally guarded: collapsing the key back to a global
primary key would lose rows when two tenants share a key, so it raises before
changing the schema in that case.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_scope_idempotency_key_by_tenant"
down_revision: str | None = "0015_reconcile_workflow_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace the global key primary key with a tenant-scoped key."""
    with op.batch_alter_table("idempotency_keys", recreate="always") as batch:
        batch.drop_constraint("pk_idempotency_keys", type_="primary")
        batch.create_primary_key("pk_idempotency_keys", ["tenant_id", "key"])


def downgrade() -> None:
    """Restore a global key primary key only when no rows would collide."""
    bind = op.get_bind()
    table = sa.table(
        "idempotency_keys",
        sa.column("key", sa.String(length=128)),
        sa.column("tenant_id", sa.Uuid()),
    )
    duplicate = bind.execute(
        sa.select(table.c.key).group_by(table.c.key).having(sa.func.count() > 1).limit(1)
    ).first()
    if duplicate is not None:
        raise RuntimeError("cannot downgrade idempotency_keys: a key is used by multiple tenants")

    with op.batch_alter_table("idempotency_keys", recreate="always") as batch:
        batch.drop_constraint("pk_idempotency_keys", type_="primary")
        batch.create_primary_key("pk_idempotency_keys", ["key"])
