"""Portable SQL adapter for break-glass grants (FM-035 / P11-US10).

Implements ``app.domain.ports.BreakGlassGrantRepository``. Grants are
append-mostly: ``add`` stages a minted grant, ``save`` persists the
revocation fields (the only mutation), and nothing ever deletes a row
so the override history stays queryable for audit.

Every method takes the grantor's ``tenant_id`` first and every query
includes ``WHERE tenant_id = :tenant_id`` (AGENTS.md R10/R12).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.break_glass import BreakGlassGrant, BreakGlassScope
from app.infrastructure.persistence.models import BreakGlassGrant as BreakGlassGrantRow


def _as_aware(value: datetime) -> datetime:
    """Return ``value`` as a UTC-aware datetime.

    Why: SQLite does not store tzinfo, so ``DateTime(timezone=True)``
    columns round-trip as naive datetimes while the domain clock is
    always aware. Normalizing at the adapter boundary keeps the
    domain's time comparisons (``is_active``, ``check``) total.
    PostgreSQL returns aware values unchanged.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_naive_or_none(value: datetime | None) -> datetime | None:
    """Mirror of :func:`_as_aware` for the nullable revocation timestamp."""
    return None if value is None else _as_aware(value)


def _to_domain(row: BreakGlassGrantRow) -> BreakGlassGrant:
    """Reconstruct the immutable grant aggregate from its ORM row."""
    return BreakGlassGrant(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        scope=BreakGlassScope(row.scope),
        reason=row.reason,
        granted_by=row.granted_by,
        granted_at=_as_aware(row.granted_at),
        expires_at=_as_aware(row.expires_at),
        revoked_at=_as_naive_or_none(row.revoked_at),
        revoked_by=row.revoked_by,
    )


class SqlBreakGlassGrantRepository:
    """Tenant-scoped durable break-glass grant store."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, grant: BreakGlassGrant) -> None:
        """Stage a freshly minted grant in the caller's transaction."""
        self._s.add(
            BreakGlassGrantRow(
                id=grant.id,
                tenant_id=grant.tenant_id,
                user_id=grant.user_id,
                scope=grant.scope.value,
                reason=grant.reason,
                granted_by=grant.granted_by,
                granted_at=grant.granted_at,
                expires_at=grant.expires_at,
                revoked_at=grant.revoked_at,
                revoked_by=grant.revoked_by,
            )
        )
        # Flush so a same-transaction read (grant-then-check in tests
        # and in the grant-then-use acceptance flow) observes the row
        # even when the session runs with autoflush disabled.
        await self._s.flush()

    async def get(self, tenant_id: UUID, grant_id: UUID) -> BreakGlassGrant | None:
        """Fetch one grant by id; ``None`` for missing/cross-tenant ids."""
        row = (
            await self._s.execute(
                select(BreakGlassGrantRow).where(
                    BreakGlassGrantRow.tenant_id == tenant_id,
                    BreakGlassGrantRow.id == grant_id,
                )
            )
        ).scalar_one_or_none()
        return _to_domain(row) if row else None

    async def save(self, grant: BreakGlassGrant) -> None:
        """Persist the revocation fields of an existing grant."""
        row = (
            await self._s.execute(
                select(BreakGlassGrantRow).where(
                    BreakGlassGrantRow.tenant_id == grant.tenant_id,
                    BreakGlassGrantRow.id == grant.id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return
        row.revoked_at = grant.revoked_at
        row.revoked_by = grant.revoked_by

    async def active_grant_for(
        self, tenant_id: UUID, user_id: UUID, scope: BreakGlassScope, *, now: datetime
    ) -> BreakGlassGrant | None:
        """Return the live grant for ``(user, scope)``, if any."""
        grant = await self.grant_for(tenant_id, user_id, scope)
        if grant is None or not grant.is_active(now=now):
            return None
        return grant

    async def grant_for(
        self, tenant_id: UUID, user_id: UUID, scope: BreakGlassScope
    ) -> BreakGlassGrant | None:
        """Return the most-recent grant for ``(user, scope)``.

        Most-recent by ``granted_at`` (then id for determinism) so the
        denial path can report revoked-vs-expired precisely.
        """
        rows = (
            (
                await self._s.execute(
                    select(BreakGlassGrantRow)
                    .where(
                        BreakGlassGrantRow.tenant_id == tenant_id,
                        BreakGlassGrantRow.user_id == user_id,
                        BreakGlassGrantRow.scope == scope.value,
                    )
                    .order_by(
                        BreakGlassGrantRow.granted_at.desc(),
                        BreakGlassGrantRow.id.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        return _to_domain(rows[0])


__all__ = ["SqlBreakGlassGrantRepository"]
