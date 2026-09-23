"""SQL adapter for the legal-hold port (P08-US1, US2 / FM-039).

Implements :class:`app.domain.ports.LegalHoldRepository` over the
``legal_holds`` table. Every query carries ``WHERE tenant_id = :tenant_id``;
cross-tenant case ids surface as ``None`` for reads and as
``Conflict``-free no-ops for mutations so the API never confirms
cross-tenant existence (Constitution V).

The adapter also satisfies the scanner's ``_CaseLookup`` Protocol by
providing :meth:`is_under_legal_hold` — a convenience over
``get_active`` so the SLA scanner does not need a separate case
lookup port.

# INTEGRATION_SEAM: legal_hold SQL adapter

The local adapter is SQLAlchemy/SQLite-Postgres portable. The Azure
SQL adapter implements the same ``LegalHoldRepository`` port without
schema changes (ADR-039).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import Conflict
from app.domain.legal_hold import LegalHold
from app.infrastructure.persistence.mapping import (
    legal_hold_from_domain,
    legal_hold_to_domain,
)
from app.infrastructure.persistence.models import LegalHold as LegalHoldRow


class SqlLegalHoldRepository:
    """SQL adapter for the ``LegalHoldRepository`` port.

    Tenant isolation is explicit: every method takes ``tenant_id``
    and every SQL carries ``WHERE tenant_id = :tenant_id``.

    The ``UNIQUE(tenant_id, case_id)`` constraint enforces the
    "at most one row per case" invariant at the storage layer. A
    concurrent ``place`` that races past ``get_active`` surfaces as
    ``Conflict(IntegrityError)``. A released hold's row is reused
    (updated in place) when a new hold is placed on the same case,
    preserving a single canonical row per case.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_active(self, tenant_id: UUID, case_id: UUID) -> LegalHold | None:
        """Return the active ``LegalHold`` for ``case_id`` or ``None``.

        Returns ``None`` for a missing case id, a cross-tenant case id,
        or a case with no active hold.
        """
        row = (
            await self._s.execute(
                select(LegalHoldRow).where(
                    LegalHoldRow.tenant_id == tenant_id,
                    LegalHoldRow.case_id == case_id,
                    LegalHoldRow.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return legal_hold_to_domain(row)

    async def is_under_legal_hold(self, tenant_id: UUID, case_id: UUID) -> bool:
        """Return True iff an active hold exists for ``case_id``.

        Convenience for the SLA scanner / disposition flow. Cross-tenant
        case ids return ``False`` so the caller cannot probe
        cross-tenant existence (Constitution V).
        """
        return await self.get_active(tenant_id, case_id) is not None

    async def add(self, hold: LegalHold) -> None:
        """Stage a fresh ``LegalHold`` row.

        Raises :class:`Conflict` when an active hold already exists
        for ``(tenant_id, case_id)``. If a released hold row exists for
        the same case, it is overwritten in place (the single-row-per-
        case model means the ``UNIQUE`` constraint fires on any
        existing row, released or active — we disambiguate by checking
        ``is_active``).
        """
        row = LegalHoldRow(**legal_hold_from_domain(hold))
        row.is_active = True
        self._s.add(row)
        try:
            await self._s.flush()
        except IntegrityError:
            # A row already exists for (tenant_id, case_id). Determine
            # whether it is still active — if so, this is a duplicate
            # placement and must be refused. A released row is reused.
            existing = (
                await self._s.execute(
                    select(LegalHoldRow).where(
                        LegalHoldRow.tenant_id == hold.tenant_id,
                        LegalHoldRow.case_id == hold.case_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None and existing.is_active:
                raise Conflict(f"case {hold.case_id} already has an active legal hold") from None
            # Released row — overwrite it with the new active hold.
            existing.placed_by = hold.placed_by
            existing.placed_reason = hold.placed_reason
            existing.placed_at = hold.placed_at
            existing.released_by = None
            existing.released_reason = None
            existing.released_at = None
            existing.is_active = True
            await self._s.flush()

    async def update(self, hold: LegalHold) -> None:
        """Persist a release on the existing active hold row.

        Only updates rows where ``is_active = True`` so a stale token
        cannot overwrite an already-released row (Constitution V).
        """
        stmt = select(LegalHoldRow).where(
            LegalHoldRow.tenant_id == hold.tenant_id,
            LegalHoldRow.case_id == hold.case_id,
            LegalHoldRow.is_active.is_(True),
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise Conflict(f"legal hold for case {hold.case_id} not found or already released")
        row.released_by = hold.released_by
        row.released_reason = hold.released_reason
        row.released_at = hold.released_at
        row.is_active = False
        await self._s.flush()


__all__ = ["SqlLegalHoldRepository"]
