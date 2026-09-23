"""create tenants and users

Foundational tables only — per T007 (first migration, walking-skeleton phase).
CaseType, CaseTypeVersion, Case, and downstream tables (audit, outbox,
idempotency, documents, workflow, etc.) ship in their own migrations
alongside the tasks that introduce their domain logic.

Portability notes (Constitution VI, R1, plan.md:288-303):

- All column types are SQLAlchemy generic types: ``Uuid``, ``JSON``,
  ``DateTime(timezone=True)``, ``Text``, ``String``, ``Integer``,
  ``Boolean``. No ``JSONB``, no ``UUID`` (dialect-specific), no
  ``ARRAY``, no range types.
- Identifier default is the application-side ``uuid4()`` value; the
  migration does NOT use ``gen_random_uuid()`` (PostgreSQL extension)
  so it runs on sqlite (test profile) and on Azure SQL without changes.
- Indexes are plain B-tree ``ix_*``. No ``GIN``, no ``tsvector``, no
  expression indexes that depend on dialect-specific operators.
- Naming follows ``app/db.py``'s ``NAMING_CONVENTION``: ``pk_*``,
  ``fk_*``, ``ix_*``, ``uq_*`` so the constraint names are stable and
  portable. The convention is applied at the ``MetaData`` level, so
  SQLAlchemy emits the prefixed names automatically.
- All foreign keys are tenant-scoped and cascade on tenant delete; this
  matches ``app/infrastructure/persistence/models.py`` and is enforced
  by every repository's explicit ``WHERE tenant_id = :tenant_id``.

Reversible: every operation has a matching ``downgrade`` step. The
``downgrade`` order is the reverse of ``upgrade``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_create_tenants_users"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Why: tenants is the root of the multi-tenant hierarchy. Every other
    # table has a non-null tenant_id FK with ON DELETE CASCADE. We do not
    # add a global unique constraint on tenants.key here; uniqueness is
    # enforced at the application layer in T005+ because the rule differs
    # by environment (local seed vs enterprise import).
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("key", name="uq_tenants_key"),
    )

    # Why: a B-tree index on tenants.key to accelerate the seed-time
    # lookup and the bootstrap path of every request that resolves a
    # tenant by short code.
    op.create_index("ix_tenants_key", "tenants", ["key"], unique=False)

    # Why: users.tenant_id is non-null and ON DELETE CASCADE — deleting a
    # tenant removes its users in the same transaction. Application code
    # never relies on this cascade alone; it always scopes user queries
    # by tenant_id (FM-002 / P11-US2).
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE", name="fk_users_tenant_id_tenants"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        # Why JSON (generic), not JSONB: the column stores a small list of
        # role names per user; JSONB's binary optimizations are not worth
        # the portability cost. Searches by role happen via JOIN against
        # a normalized role table when the user count warrants it (later
        # slice), not via JSONB containment.
        sa.Column("roles", sa.JSON(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
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
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_users_tenant_key"),
    )

    op.create_index("ix_users_tenant_id", "users", ["tenant_id"], unique=False)


def downgrade() -> None:
    # Reverse order: drop the dependent table first.
    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")

    op.drop_index("ix_tenants_key", table_name="tenants")
    op.drop_table("tenants")
