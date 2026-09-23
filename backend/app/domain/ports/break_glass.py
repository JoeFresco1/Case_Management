"""Break-glass grant persistence port (P11-US10 / FM-035).

The grant is the durable record of an approved exceptional-access
override. It belongs to the *grantor's* tenant (the data-owning
tenant whose administrator approved it); the grantee may operate from
a different tenant. Every method is tenant-scoped on the grantor's
tenant so a grant id from tenant A never resolves in tenant B.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.break_glass import BreakGlassGrant, BreakGlassScope
from app.domain.ports.core import TenantScoped


class BreakGlassGrantRepository(TenantScoped, Protocol):
    """Durable break-glass grant store.

    Grants are never deleted; revocation writes ``revoked_at`` /
    ``revoked_by`` so the override history stays queryable for audit.
    The SQL adapter is
    ``app.infrastructure.persistence.repositories.break_glass_repository.SqlBreakGlassGrantRepository``.
    """

    async def add(self, grant: BreakGlassGrant) -> None:
        """Persist a freshly minted grant."""

    async def get(self, tenant_id: UUID, grant_id: UUID) -> BreakGlassGrant | None:
        """Fetch one grant by id, scoped to the grantor's tenant.

        Returns ``None`` for missing or cross-tenant ids — the two
        cases are intentionally indistinguishable so the API never
        confirms cross-tenant existence (Constitution V).
        """

    async def save(self, grant: BreakGlassGrant) -> None:
        """Persist a mutated grant (revocation is the only mutation)."""

    async def active_grant_for(
        self, tenant_id: UUID, user_id: UUID, scope: BreakGlassScope, *, now: datetime
    ) -> BreakGlassGrant | None:
        """Return the live grant for ``(user, scope)``, if any.

        The fast-path used by the read-side enforcement hook.
        """

    async def grant_for(
        self, tenant_id: UUID, user_id: UUID, scope: BreakGlassScope
    ) -> BreakGlassGrant | None:
        """Return the most-recent grant for ``(user, scope)`` regardless
        of active status, so callers can report precise denial reasons
        (revoked vs expired vs never-granted).
        """


__all__ = ["BreakGlassGrantRepository"]
