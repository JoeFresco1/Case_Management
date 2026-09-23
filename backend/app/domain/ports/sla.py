"""SLA clock port (P02-US6 / FM-018 / FM-039 H0 seam).

The first slice ships a minimal ``SlaClock`` lifecycle: start, pause,
resume, complete, breach. The H0 scope is a basic clock that the
workflow engine starts via a ``start_sla`` action (T071) and that the
scanner (``app.application.sla.scan_sla``) flips to ``breached`` once
``now > due_at``. FM-039's H0 seam is *not* on this port -- the
legal-hold refusal lives on the case aggregate (see ``Case`` /
``app.domain.case``) and the application use case that closes a case.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.ports.core import TenantScoped
from app.domain.sla import SlaClock


class SlaClockRepository(TenantScoped, Protocol):
    """``SlaClock`` aggregate persistence (P02-US6 / FM-018).

    The first-slice surface is intentionally small: ``add`` to stage
    a new clock, ``get`` to read by id (tenant-scoped), ``update`` to
    persist a status flip, and ``list_due`` to drive the scanner
    (FM-018). The scanner is the only writer that transitions a
    ``running`` clock to ``breached``; the use case then writes the
    ``SlaBreached`` audit row in the same transaction so the timeline
    is consistent with the clock state (P09-US2).

    Idempotency is enforced at the **domain** layer
    (:meth:`SlaClock.mark_breached`); the repository accepts the new
    state from the aggregate. Tenant isolation is mandatory (AGENTS.md
    §3 R1, §12): every method takes ``tenant_id`` first and the SQL
    implementation must include ``WHERE tenant_id = :tenant_id``.
    """

    async def add(self, clock: SlaClock) -> None:
        """Persist a freshly started clock.

        The ``tenant_id`` comes from the aggregate. Implementations
        must insert the row in the same transaction as the audit /
        outbox rows the workflow engine staged so a partial commit
        never leaves a workflow transition without its clock
        (Constitution III).
        """

    async def get(self, tenant_id: UUID, clock_id: UUID) -> SlaClock | None:
        """Resolve a clock by id, tenant-scoped.

        Returns ``None`` for missing or cross-tenant ids -- the two
        cases are intentionally indistinguishable so the API never
        confirms cross-tenant existence (Constitution V).
        """

    async def update(self, clock: SlaClock) -> None:
        """Persist a status flip on an existing clock.

        Called by the application use case after
        :meth:`SlaClock.pause` / :meth:`SlaClock.resume` /
        :meth:`SlaClock.complete` / :meth:`SlaClock.mark_breached`.
        The aggregate carries the new state; the repository stores
        it. No optimistic-concurrency token on the clock (the
        scanner is the only writer that flips a running clock to
        breached and it does so from a single-tick transaction).
        """

    async def list_counting(
        self,
        tenant_id: UUID,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[SlaClock]:
        """Return every counting clock for ``tenant_id`` ahead of ``now``.

        Used by the warning-tier scanner
        (:func:`app.application.sla.scan_sla.scan_warning_once`)
        to discover clocks that are still ahead of ``due_at`` but
        already inside the configured ``warning_at..due_at``
        window. The breach scanner
        (:func:`app.application.sla.scan_sla.scan_once`) uses
        :meth:`list_due` instead because it only needs clocks that
        have crossed the deadline.

        The query returns counting clocks
        (``active`` / ``running`` / ``warning``) regardless of
        ``due_at`` so the warning pass can classify each clock
        against the timestamp. The aggregate's
        :meth:`SlaClock.is_in_warning_window` is the authority
        on whether a given clock is actually in the warning
        window -- the repository is the discoverer, the
        aggregate is the decider.
        """

    async def list_due(
        self,
        tenant_id: UUID,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[SlaClock]:
        """Return counting clocks whose ``due_at`` is at or before ``now``.

        Used by the SLA scanner to find candidates for breach. The
        scanner flips the clock's status to ``breached`` via the
        aggregate (which is idempotent) and writes the
        ``SlaBreached`` audit row. Only counting clocks (``active``
        or ``warning``) are returned -- paused and terminal clocks
        are skipped by definition. The legacy ``running`` alias is
        treated identically to ``active`` for backwards
        compatibility with rows persisted by the H0 scanner.

        Tenant-scoped (``WHERE tenant_id = :tenant_id``); a
        cross-tenant ``now`` cannot widen the result set. The
        default cap protects the scanner from accidentally
        scanning a large backlog in a single tick; the production
        worker may pass a larger ``limit``.
        """

    async def list_for_case(
        self,
        tenant_id: UUID,
        case_id: UUID,
    ) -> list[SlaClock]:
        """Return every clock bound to ``case_id``, tenant-scoped.

        Used by the case workspace read path to project the
        ``sla_state`` summary field (P02-US6 / FM-018). The list is
        unsorted -- the projection sorts as needed. There is no
        pagination cap because a single case carries a small,
        bounded number of clocks (one per workflow action that
        starts an SLA). Tenant isolation is enforced by the same
        ``WHERE tenant_id = :tenant_id`` clause every other
        repository method uses.
        """


__all__ = [
    "SlaClockRepository",
]
