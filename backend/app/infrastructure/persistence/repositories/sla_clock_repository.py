"""SLA clock SQL repository (P02-US6 / FM-018).

Implements the ``SlaClockRepository`` protocol with explicit tenant
isolation. The row layout lives in :mod:`app.infrastructure.persistence.models`
and mirrors the domain ``SlaClock`` aggregate.

# INTEGRATION_SEAM: relational DB

This is the local adapter. The future ``AzureSqlSlaClockRepository``
implements the same protocol and is wired by ``app.deps`` when the
env demands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sla import SlaClock, SlaClockStatus
from app.infrastructure.persistence.models import SlaClock as SlaClockRow


def _to_domain(row: SlaClockRow) -> SlaClock:
    """Translate the ORM row into a :class:`SlaClock` aggregate.

    The mapping is field-for-field: ``status`` is normalized to the
    canonical ``active`` value (the legacy ``running`` alias
    deserializes to ``active`` so the domain layer never has to
    branch on the wire value). ``breach_action`` and ``metadata``
    keep their JSON shape unchanged; ``escalation_on_warning`` and
    ``escalation_on_breach`` are read straight from the row.
    """
    metadata = dict(row.metadata_json or {})
    warning_at_raw = metadata.pop("warning_at", None)
    warning_at = datetime.fromisoformat(warning_at_raw) if warning_at_raw else None
    escalation_on_warning = metadata.pop("escalation_on_warning", None)
    escalation_on_breach = metadata.pop("escalation_on_breach", None)
    return SlaClock(
        id=row.id,
        tenant_id=row.tenant_id,
        case_id=row.case_id,
        clock_key=row.clock_key,
        started_at=row.started_at,
        due_at=row.due_at,
        status=SlaClockStatus(row.status),
        paused_at=row.paused_at,
        warning_at=warning_at,
        breach_action=row.breach_action,
        escalation_on_warning=escalation_on_warning,
        escalation_on_breach=escalation_on_breach,
        metadata=metadata or None,
    )


def _to_row(clock: SlaClock) -> dict[str, Any]:
    """Map a :class:`SlaClock` aggregate to ORM row kwargs.

    Centralizes the field rename (``metadata`` -> ``metadata_json``)
    and the status normalization. ``escalation_on_warning`` /
    ``escalation_on_breach`` are not yet on the ORM row; the H0
    column set is the minimum needed for the breach pass and the
    H0 audit row. The H1 widening adds the new columns; until then
    the additional fields ride on the row's existing JSON columns
    via a synthesized ``metadata_json`` payload so the projection
    round-trips without losing state.
    """
    metadata: dict[str, Any] = dict(clock.metadata or {})
    if clock.warning_at is not None:
        metadata.setdefault("warning_at", clock.warning_at.isoformat())
    if clock.escalation_on_warning is not None:
        metadata.setdefault("escalation_on_warning", clock.escalation_on_warning)
    if clock.escalation_on_breach is not None:
        metadata.setdefault("escalation_on_breach", clock.escalation_on_breach)
    return {
        "id": clock.id,
        "tenant_id": clock.tenant_id,
        "case_id": clock.case_id,
        "clock_key": clock.clock_key,
        "started_at": clock.started_at,
        "due_at": clock.due_at,
        "status": clock.status.value,
        "paused_at": clock.paused_at,
        "breach_action": clock.breach_action,
        "metadata_json": metadata or None,
    }


class SqlSlaClockRepository:
    """SQLAlchemy adapter for :class:`SlaClock`.

    Tenant isolation is mandatory (AGENTS.md §3 R1, §12): every
    query includes ``WHERE tenant_id = :tenant_id``. The scanner
    passes the same tenant id; the router passes the actor's
    tenant id.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, clock: SlaClock) -> None:
        """Persist a freshly started clock (P02-US1 / T071)."""
        self._s.add(SlaClockRow(**_to_row(clock)))

    async def get(self, tenant_id: UUID, clock_id: UUID) -> SlaClock | None:
        """Resolve a clock by id, tenant-scoped."""
        stmt = select(SlaClockRow).where(
            SlaClockRow.tenant_id == tenant_id,
            SlaClockRow.id == clock_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _to_domain(row) if row else None

    async def update(self, clock: SlaClock) -> None:
        """Persist a status flip on an existing clock.

        Compare-and-swap on the row's natural identity (id +
        tenant_id); the scanner is the only writer that flips a
        counting clock to ``breached`` and it does so from a
        single-tick transaction so a multi-tick race is impossible
        without a concurrent scanner.
        """
        stmt = select(SlaClockRow).where(
            SlaClockRow.tenant_id == clock.tenant_id,
            SlaClockRow.id == clock.id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise LookupError(f"sla clock {clock.id} not found in tenant {clock.tenant_id}")
        row.status = clock.status.value
        row.due_at = clock.due_at
        row.paused_at = clock.paused_at
        row.breach_action = clock.breach_action
        metadata: dict[str, Any] = dict(clock.metadata or {})
        if clock.warning_at is not None:
            metadata.setdefault("warning_at", clock.warning_at.isoformat())
        if clock.escalation_on_warning is not None:
            metadata.setdefault("escalation_on_warning", clock.escalation_on_warning)
        if clock.escalation_on_breach is not None:
            metadata.setdefault("escalation_on_breach", clock.escalation_on_breach)
        row.metadata_json = metadata or None

    async def list_due(
        self,
        tenant_id: UUID,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[SlaClock]:
        """Return counting clocks whose ``due_at`` is at or before ``now``."""
        counting = (
            SlaClockStatus.ACTIVE.value,
            SlaClockStatus.RUNNING.value,
            SlaClockStatus.WARNING.value,
        )
        stmt = (
            select(SlaClockRow)
            .where(
                SlaClockRow.tenant_id == tenant_id,
                SlaClockRow.status.in_(counting),
                SlaClockRow.due_at <= now,
            )
            .order_by(SlaClockRow.due_at.asc())
            .limit(limit)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_to_domain(r) for r in rows]

    async def list_counting(
        self,
        tenant_id: UUID,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[SlaClock]:
        """Return every counting clock for ``tenant_id`` ahead of ``now``.

        Used by the warning-tier scanner. The aggregate's
        ``is_in_warning_window`` decides whether each clock is
        actually in the warning tier; the repository is the
        discoverer.
        """
        counting = (
            SlaClockStatus.ACTIVE.value,
            SlaClockStatus.RUNNING.value,
            SlaClockStatus.WARNING.value,
        )
        stmt = (
            select(SlaClockRow)
            .where(
                SlaClockRow.tenant_id == tenant_id,
                SlaClockRow.status.in_(counting),
            )
            .order_by(SlaClockRow.due_at.asc())
            .limit(limit)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_to_domain(r) for r in rows]

    async def list_for_case(
        self,
        tenant_id: UUID,
        case_id: UUID,
    ) -> list[SlaClock]:
        """Return every clock bound to ``case_id``, tenant-scoped."""
        stmt = select(SlaClockRow).where(
            SlaClockRow.tenant_id == tenant_id,
            SlaClockRow.case_id == case_id,
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_to_domain(r) for r in rows]


__all__ = ["SqlSlaClockRepository"]
