"""Portable SQL adapter for per-tenant reference format overrides (FM-009).

Implements the ``ReferenceFormatRepository`` Protocol from
``app.domain.reference_format``: one row per tenant, keyed on
``tenant_id``. Every query includes ``WHERE tenant_id = :tenant_id``
(AGENTS.md R10/R12). ``upsert`` is an update-or-insert so reconfiguring
a template never violates the per-tenant unique constraint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.reference_format import ReferenceFormat
from app.infrastructure.persistence.models import ReferenceFormat as ReferenceFormatRow


class SqlReferenceFormatRepository:
    """Tenant-scoped reference-template store."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get_for_tenant(self, tenant_id: UUID) -> ReferenceFormat | None:
        """Return the tenant's override template, or ``None`` if none."""
        row = (
            await self._s.execute(
                select(ReferenceFormatRow).where(ReferenceFormatRow.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return ReferenceFormat(tenant_id=row.tenant_id, template=row.template)

    async def upsert(self, format: ReferenceFormat, *, now: datetime) -> None:
        """Write (or replace) the tenant's override template."""
        row = (
            await self._s.execute(
                select(ReferenceFormatRow).where(ReferenceFormatRow.tenant_id == format.tenant_id)
            )
        ).scalar_one_or_none()
        occurred = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        if row is None:
            self._s.add(
                ReferenceFormatRow(
                    id=uuid4(),
                    tenant_id=format.tenant_id,
                    template=format.template,
                    created_at=occurred,
                    updated_at=occurred,
                )
            )
        else:
            row.template = format.template
            row.updated_at = occurred
        # Flush so a same-transaction read (e.g. configure-then-reset,
        # or the issuance right after configuration) observes the row
        # even when the session runs with autoflush disabled.
        await self._s.flush()


__all__ = ["SqlReferenceFormatRepository"]
