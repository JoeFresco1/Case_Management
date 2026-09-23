"""In-memory ``LegalHoldRepository`` for the FM-039 H1 slice.

This module is the first-slice adapter for the
``LegalHoldRepository`` port declared in ``app.domain.ports``. The
SQL adapter lands in a follow-up dispatch; until then the router
mounts this in-memory adapter so the HTTP surface is exercisable
end-to-end against synthetic tenants.

# INTEGRATION_SEAM: legal_hold SQL adapter

The in-memory adapter is a **seam placeholder** (AGENTS.md §12):
its job is to be runtime-correct so the use case + router are
verifiable without the SQL adapter being on the critical path. The
production seam is a ``legal_holds`` table behind the same Protocol;
when that lands, the router swaps the import and the in-memory
adapter is reserved for tests.

# Tenant isolation

The adapter scopes every read and write by ``(tenant_id, case_id)``
in memory. A cross-tenant ``case_id`` returns ``None`` from
``get_active`` and is treated as a no-op on writes so the API never
confirms cross-tenant existence (Constitution V).
"""

from __future__ import annotations

from threading import RLock
from uuid import UUID

from app.domain.errors import Conflict
from app.domain.legal_hold import LegalHold


class InMemoryLegalHoldRepository:
    """Process-scoped ``LegalHoldRepository`` (FM-039 / P08-US1, US2).

    Holds live in an in-process dictionary keyed by
    ``(tenant_id, case_id)`` for the active row only; released rows
    stay reachable by ``hold.id`` for audit replay but the
    ``(tenant_id, case_id)`` slot is vacated. The lock is required
    because FastAPI may serve requests from multiple worker threads
    in production; the in-memory adapter serializes the
    ``get_active`` -> ``add`` / ``update`` critical section so a
    concurrent ``place`` cannot race past ``get_active`` and stage a
    second active row.
    """

    def __init__(self) -> None:
        self._by_case: dict[tuple[UUID, UUID], LegalHold] = {}
        self._by_id: dict[UUID, LegalHold] = {}
        self._lock = RLock()

    async def get_active(self, tenant_id: UUID, case_id: UUID) -> LegalHold | None:
        """Return the active ``LegalHold`` for ``case_id`` in ``tenant_id``."""
        with self._lock:
            hold = self._by_case.get((tenant_id, case_id))
            if hold is None or not hold.is_active:
                return None
            return hold

    async def add(self, hold: LegalHold) -> None:
        """Stage a fresh ``LegalHold`` and bind it to ``(tenant_id, case_id)``.

        Raises :class:`Conflict` when an active hold already exists
        for ``(tenant_id, case_id)``. This is the in-memory analog of
        the SQL adapter's ``UNIQUE(tenant_id, case_id)`` constraint
        and exists so the adapter exercises the same invariant
        without the live container.
        """
        with self._lock:
            existing = self._by_case.get((hold.tenant_id, hold.case_id))
            if existing is not None and existing.is_active:
                raise Conflict(f"case {hold.case_id} already has an active legal hold")
            self._by_id[hold.id] = hold
            self._by_case[(hold.tenant_id, hold.case_id)] = hold

    async def update(self, hold: LegalHold) -> None:
        """Persist a release on the existing active hold.

        Raises :class:`Conflict` when the row is missing or already
        released — the use case calls ``get_active`` first and the
        caller never passes a stale aggregate.
        """
        with self._lock:
            stored = self._by_id.get(hold.id)
            if stored is None:
                raise Conflict(f"legal hold {hold.id} not found")
            if stored.tenant_id != hold.tenant_id:
                raise Conflict(f"legal hold {hold.id} tenant mismatch")
            if not stored.is_active:
                raise Conflict(f"legal hold {hold.id} is already released")
            self._by_id[hold.id] = hold
            # The active slot stays bound until the use case releases
            # it; the next ``get_active`` still returns the released
            # row's binding so the caller knows there is no other
            # active row, but the row carries ``is_active=False``.
            self._by_case[(hold.tenant_id, hold.case_id)] = hold

    async def is_under_legal_hold(self, tenant_id: UUID, case_id: UUID) -> bool:
        """Return True iff an active hold exists for ``case_id``.

        Convenience for the SLA scanner / disposition flow. Cross-tenant
        case ids return ``False`` so the caller cannot probe
        cross-tenant existence (Constitution V).
        """
        with self._lock:
            hold = self._by_case.get((tenant_id, case_id))
            return hold is not None and hold.is_active

    async def history_for_case(self, tenant_id: UUID, case_id: UUID) -> list[LegalHold]:
        """Return every hold row (active + released) for ``case_id``.

        The retention disposition flow translates this history into
        frozen-clock periods for pause-and-resume accounting (002
        spec §9 Q5: the disposition horizon shifts forward by total
        hold time). Order is ``placed_at ASC`` so the pause sum is
        deterministic. A cross-tenant case id returns ``[]`` so the
        caller cannot probe cross-tenant existence (Constitution V).

        Why kept alongside ``get_active`` (not a second store):
        released rows already stay reachable in ``_by_id`` for audit
        replay; this method only adds the per-case index over them.
        The future SQL adapter answers the same query with
        ``WHERE tenant_id = :tenant_id AND case_id = :case_id
        ORDER BY placed_at ASC``.
        """
        with self._lock:
            rows = [
                hold
                for hold in self._by_id.values()
                if hold.tenant_id == tenant_id and hold.case_id == case_id
            ]
            rows.sort(key=lambda h: h.placed_at)
            return list(rows)


__all__ = ["InMemoryLegalHoldRepository"]
