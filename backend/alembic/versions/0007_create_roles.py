"""create roles and role_assignments tables

Foundation for P11-US4 / FM-048 (role and permission administration).
Security administrators define roles (a name + a list of permission
strings), assign users to roles, and revoke — revocation is a delete
on ``role_assignments``; the policy reads the table on every
``evaluate`` call so the next request reflects the change.

# Schema

- ``roles``: one row per named role. ``permissions`` is a JSON array of
  ``Action.value`` strings (e.g., "case.create"). The unique
  ``(tenant_id, name)`` constraint prevents two roles from sharing a
  name inside the same tenant.
- ``role_assignments``: the user-belongs-to-role fact. The composite
  primary key on ``(tenant_id, user_id, role_id)`` is portable and
  makes ``revoke`` a single-row delete.

# Portability

All column types are SQLAlchemy generic types (Uuid, JSON,
DateTime(timezone=True)). No ``gen_random_uuid()``, no JSONB, no
``ON CONFLICT`` clauses. Both SQLite (tests) and PostgreSQL/Azure SQL
accept the table shape.

# Reversibility

``downgrade`` drops the indexes, then the tables in reverse dependency
order (role_assignments -> roles).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_create_roles"
down_revision: str | None = "0006_create_configuration_and_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- roles ----------------------------------------------------------
    # Why unique (tenant_id, name): the role name is the stable lookup
    # key within a tenant (admins reference roles by name). Two roles
    # sharing a name would silently shadow authorization.
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_roles_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        # Why JSON list of strings: the permission set is bounded by the
        # closed ``Action`` enum and small (single-digit entries).
        # Storing strings rather than enum ints keeps the column
        # human-readable for operators inspecting the table.
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("is_reserved", sa.Boolean(), nullable=False, server_default=sa.false()),
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
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_roles_tenant_name"),
    )
    op.create_index(
        "ix_roles_tenant_id",
        "roles",
        ["tenant_id"],
        unique=False,
    )

    # ---- role_assignments ----------------------------------------------
    # Why composite PK on (tenant_id, user_id, role_id): a user can hold
    # each role at most once; the constraint is portable across SQLite
    # and PostgreSQL (no array types, no exclusion constraints).
    op.create_table(
        "role_assignments",
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenants.id",
                ondelete="CASCADE",
                name="fk_role_assignments_tenant_id_tenants",
            ),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey(
                "users.id",
                ondelete="CASCADE",
                name="fk_role_assignments_user_id_users",
            ),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            sa.Uuid(),
            sa.ForeignKey(
                "roles.id",
                ondelete="CASCADE",
                name="fk_role_assignments_role_id_roles",
            ),
            nullable=False,
        ),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("assigned_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "role_id", name="pk_role_assignments"),
    )
    op.create_index(
        "ix_role_assignments_tenant_user",
        "role_assignments",
        ["tenant_id", "user_id"],
        unique=False,
    )
    op.create_index(
        "ix_role_assignments_tenant_role",
        "role_assignments",
        ["tenant_id", "role_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_role_assignments_tenant_role", table_name="role_assignments")
    op.drop_index("ix_role_assignments_tenant_user", table_name="role_assignments")
    op.drop_table("role_assignments")

    op.drop_index("ix_roles_tenant_id", table_name="roles")
    op.drop_table("roles")
