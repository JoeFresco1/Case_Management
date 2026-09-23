"""Role + assignment SQL repository.

SQLAlchemy adapter that satisfies ``app.domain.ports.RoleRepository``.
The policy reads ``permissions_for_user`` on every evaluation so
revocation is immediate — no cache invalidation needed.

Every method takes ``tenant_id`` first and the WHERE clause includes it
on every read (AGENTS.md §4.R10, plan §4.R10, P11-US2).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import Conflict
from app.domain.role import Role
from app.infrastructure.persistence.models import (
    Role as RoleRow,
)
from app.infrastructure.persistence.models import (
    RoleAssignment as RoleAssignmentRow,
)


def _role_from_row(row: RoleRow) -> Role:
    return Role(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        permissions=frozenset(row.permissions or []),
        is_reserved=row.is_reserved,
        created_at=row.created_at,
    )


class SqlRoleRepository:
    """Tenant-scoped role + assignment store.

    Revocation (``revoke``) deletes the ``role_assignments`` row. The
    policy's ``evaluate`` re-reads the table on every call so the next
    request is denied. There is no in-memory cache here on purpose.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # ----- Role CRUD ------------------------------------------------------

    async def get(self, tenant_id: UUID, role_id: UUID) -> Role | None:
        stmt = select(RoleRow).where(RoleRow.tenant_id == tenant_id, RoleRow.id == role_id)
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _role_from_row(row) if row else None

    async def get_by_name(self, tenant_id: UUID, name: str) -> Role | None:
        stmt = select(RoleRow).where(RoleRow.tenant_id == tenant_id, RoleRow.name == name)
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _role_from_row(row) if row else None

    async def list(self, tenant_id: UUID) -> Sequence[Role]:
        stmt = select(RoleRow).where(RoleRow.tenant_id == tenant_id).order_by(RoleRow.name.asc())
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_role_from_row(r) for r in rows]

    async def add(self, role: Role) -> None:
        try:
            self._s.add(
                RoleRow(
                    id=role.id,
                    tenant_id=role.tenant_id,
                    name=role.name,
                    description=role.description,
                    permissions=list(role.permissions),
                    is_reserved=role.is_reserved,
                    created_at=role.created_at,
                    updated_at=role.created_at,
                )
            )
            await self._s.flush()
        except IntegrityError as exc:
            # The (tenant_id, name) unique constraint is the most likely
            # collision source for the application use case. Surface it
            # as a domain Conflict rather than leaking SQLAlchemy.
            raise Conflict(f"role {role.name!r} already exists in this tenant") from exc

    async def update_permissions(
        self, tenant_id: UUID, role_id: UUID, permissions: frozenset[str]
    ) -> None:
        # Filter on tenant_id as well so a wrong-tenant role_id cannot
        # be mutated by an admin who guessed the id (P11-US2).
        stmt = select(RoleRow).where(RoleRow.tenant_id == tenant_id, RoleRow.id == role_id)
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        row.permissions = sorted(permissions)
        row.updated_at = datetime.now(tz=UTC)
        await self._s.flush()

    async def delete(self, tenant_id: UUID, role_id: UUID) -> None:
        stmt = delete(RoleRow).where(RoleRow.tenant_id == tenant_id, RoleRow.id == role_id)
        await self._s.execute(stmt)

    # ----- Assignment ----------------------------------------------------

    async def assign(
        self,
        tenant_id: UUID,
        user_id: UUID,
        role_id: UUID,
        *,
        assigned_by: UUID | None,
        assigned_at: datetime,
    ) -> None:
        # ``INSERT OR IGNORE`` semantics: a duplicate assign is a no-op
        # so retries (e.g., idempotent replay) do not raise. We rely on
        # the composite primary key as the guard.
        self._s.add(
            RoleAssignmentRow(
                tenant_id=tenant_id,
                user_id=user_id,
                role_id=role_id,
                assigned_at=assigned_at,
                assigned_by=assigned_by,
            )
        )
        try:
            await self._s.flush()
        except IntegrityError:
            # already assigned; treat as a no-op (idempotent).
            await self._s.rollback()

    async def revoke(self, tenant_id: UUID, user_id: UUID, role_id: UUID) -> None:
        stmt = delete(RoleAssignmentRow).where(
            RoleAssignmentRow.tenant_id == tenant_id,
            RoleAssignmentRow.user_id == user_id,
            RoleAssignmentRow.role_id == role_id,
        )
        await self._s.execute(stmt)

    async def permissions_for_user(self, tenant_id: UUID, user_id: UUID) -> frozenset[str]:
        """Live union of every permission granted to ``user_id``.

        Two inner joins: ``role_assignments`` -> ``roles``, then collect
        ``roles.permissions``. Returns an empty frozenset (deny by
        default) when the user has no assignments.
        """
        stmt = (
            select(RoleRow.permissions)
            .join(
                RoleAssignmentRow,
                RoleAssignmentRow.role_id == RoleRow.id,
            )
            .where(RoleAssignmentRow.tenant_id == tenant_id)
            .where(RoleAssignmentRow.user_id == user_id)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        out: set[str] = set()
        for perms in rows:
            for p in perms or []:
                out.add(p)
        return frozenset(out)


__all__ = ["SqlRoleRepository"]
