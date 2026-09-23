"""Authorization and exceptional-access ORM models.

Roles, role assignments (P11-US4 / FM-048), and break-glass grants
(P11 / FM-035). Split verbatim from the pre-split
``persistence/models.py`` (AGENTS.md §4.R2).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

from .base import TimestampMixin

# ---------------------------------------------------------------------------
# Roles + assignments (P11-US4 / FM-048).
#
# The seeded JSON roles on ``users.roles`` stay as the local-dev identity
# hook. The authoritative role/permission map for the authorization
# policy lives here: ``roles`` (one row per named role, with a JSON list
# of ``Action.value`` strings) and ``role_assignments`` (the
# user-belongs-to-role fact). Revocation is a row delete; the policy
# reads ``role_assignments`` live so the next request reflects the
# change without any cache invalidation.
# ---------------------------------------------------------------------------


class Role(Base, TimestampMixin):
    """A named bundle of permissions.

    Why JSON for ``permissions``: the list is small and bounded by the
    closed ``Action`` enum. SQLAlchemy's generic ``JSON`` column is
    portable on SQLite and PostgreSQL/Azure SQL and avoids the
    AGENTS.md §16.1 ban on JSONB.

    Why a unique ``(tenant_id, name)`` constraint: role names are the
    stable lookup key within a tenant (admins reference roles by name
    in the API). A duplicate name in the same tenant would silently
    shadow authorization; the constraint prevents the bug from being
    persisted.
    """

    __tablename__ = "roles"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    permissions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    is_reserved: Mapped[bool] = mapped_column(default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_roles_tenant_name"),
        Index("ix_roles_tenant", "tenant_id"),
    )


# ---------------------------------------------------------------------------
# Roles + assignments (P11-US4 / FM-048).


class RoleAssignment(Base):
    """A user-holds-a-role fact inside a tenant.

    The composite primary key is the canonical "unique by
    (tenant, user, role)" tuple. Deleting the row is revocation; the
    policy reads this table on every ``evaluate`` call so revocation is
    immediate (P11-US4).
    """

    __tablename__ = "role_assignments"

    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    assigned_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "user_id", "role_id", name="pk_role_assignments"),
        Index("ix_role_assignments_tenant_user", "tenant_id", "user_id"),
        Index("ix_role_assignments_tenant_role", "tenant_id", "role_id"),
    )


# ---------------------------------------------------------------------------
# Break-glass grants (P11 / FM-035 / P11-US10)
#
# A grant belongs to the *grantor's* tenant (the security administrator
# who approved the override, i.e. the data-owning tenant). The grantee
# (``user_id``) may operate from a different tenant — that is the point
# of the cross-tenant scopes. The audit row for every action under the
# grant lands in the grantor's tenant (see ``record_break_glass_action``).
# Grants are never deleted; revocation sets ``revoked_at``/``revoked_by``
# so the full override history stays queryable.
# ---------------------------------------------------------------------------


class BreakGlassGrant(Base):
    """Durable time-bounded exceptional-access override (FM-035)."""

    __tablename__ = "break_glass_grants"

    id: Mapped[UUID] = mapped_column(SAUuid(), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        SAUuid(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by: Mapped[UUID] = mapped_column(SAUuid(), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[UUID | None] = mapped_column(SAUuid(), nullable=True)

    __table_args__ = (
        Index("ix_break_glass_grants_tenant_user_scope", "tenant_id", "user_id", "scope"),
    )
