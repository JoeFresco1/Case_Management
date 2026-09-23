"""LegalHold port -- per-case hold lifecycle (P08-US1, US2 / FM-039)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.domain.legal_hold import LegalHold
from app.domain.ports.core import TenantScoped


class LegalHoldRepository(TenantScoped, Protocol):
    """Per-case legal hold persistence (FM-039 / P08-US1, US2).

    A case has at most one **active** hold (a single binary flag per
    case -- P08 pillar Q1, v1 decision). ``get_active`` returns the
    active row or ``None``; ``place`` raises :class:`Conflict` if an
    active hold already exists; ``release`` writes the release
    fields on the existing active row.

    Tenant isolation is mandatory (AGENTS.md §3 R1, §12): every
    method takes ``tenant_id`` first and the SQL implementation must
    include ``WHERE tenant_id = :tenant_id``. Cross-tenant case ids
    surface as ``None`` for reads and as no-op writes for mutations
    so the API never confirms cross-tenant existence (Constitution V).
    """

    async def get_active(self, tenant_id: UUID, case_id: UUID) -> LegalHold | None:
        """Return the active ``LegalHold`` for ``case_id`` or ``None``.

        Returns ``None`` for a missing case id, a cross-tenant case id,
        or a case with no active hold -- the three cases are
        intentionally indistinguishable at this layer so the use case
        can map them to ``NotFound`` vs ``HoldNotPresent``.
        """

    async def add(self, hold: LegalHold) -> None:
        """Persist a fresh ``LegalHold``.

        Implementations MUST enforce ``UNIQUE(tenant_id, case_id)``
        for active holds so a concurrent ``place`` cannot race past
        ``get_active`` and stage a second active row. The use case
        resolves the active lookup first; the unique index is the
        defense-in-depth layer (Constitution V).
        """

    async def update(self, hold: LegalHold) -> None:
        """Persist a release on an existing active hold.

        The use case calls this after :meth:`LegalHold.release`
        produces a new aggregate carrying ``released_by`` /
        ``released_reason`` / ``released_at``. Implementations MUST
        only update rows where ``released_at IS NULL`` so a stale
        token cannot overwrite an already-released row (Constitution
        V -- no silent overwrites).
        """

    async def is_under_legal_hold(self, tenant_id: UUID, case_id: UUID) -> bool:
        """Return True iff an active hold exists for ``case_id``.

        Convenience for the SLA scanner and retention disposition flow,
        which only need the boolean flag, not the full aggregate. A
        cross-tenant case id returns ``False`` so the caller cannot
        probe cross-tenant existence (Constitution V).
        """


__all__ = [
    "LegalHoldRepository",
]
