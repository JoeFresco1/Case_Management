"""Import-mapping SQL repository (P14-US3 / FM-026).

Tenant-scoped CRUD over ``import_mappings``. Every query carries
``WHERE tenant_id = :tenant_id``; cross-tenant ids surface as None.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import Conflict
from app.domain.import_mapping import ImportMapping, ImportMappingStatus
from app.infrastructure.persistence.models import ImportMapping as ImportMappingRow


def _normalize_dt(value: datetime | None) -> datetime | None:
    """Ensure domain datetimes are tz-aware (SQLite returns naive)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _to_domain(row: ImportMappingRow) -> ImportMapping:
    return ImportMapping(
        id=row.id,
        tenant_id=row.tenant_id,
        key=row.key,
        version_number=row.version_number,
        mapping_json=dict(row.mapping_json),
        status=ImportMappingStatus(row.status),
        published_at=_normalize_dt(row.published_at),
        published_by=row.published_by,
        created_at=_normalize_dt(row.created_at),
        updated_at=_normalize_dt(row.updated_at),
    )


class SqlImportMappingRepository:
    """SQL adapter for the import-mapping registry."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, tenant_id: UUID, mapping_id: UUID) -> ImportMapping | None:
        """Return the mapping row for ``mapping_id`` in ``tenant_id``."""
        stmt = select(ImportMappingRow).where(
            ImportMappingRow.tenant_id == tenant_id,
            ImportMappingRow.id == mapping_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _to_domain(row) if row else None

    async def get_by_id(self, tenant_id: UUID, mapping_id: UUID) -> ImportMapping | None:
        """Alias for :meth:`get` (consistent with other repositories)."""
        return await self.get(tenant_id, mapping_id)

    async def list_for_key(self, tenant_id: UUID, key: str) -> Sequence[ImportMapping]:
        """List all versions for ``key`` ordered by version_number."""
        stmt = (
            select(ImportMappingRow)
            .where(
                ImportMappingRow.tenant_id == tenant_id,
                ImportMappingRow.key == key,
            )
            .order_by(ImportMappingRow.version_number)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_to_domain(r) for r in rows]

    async def list_published_for_key(self, tenant_id: UUID, key: str) -> Sequence[ImportMapping]:
        """List published versions for ``key`` ordered by version_number desc."""
        stmt = (
            select(ImportMappingRow)
            .where(
                ImportMappingRow.tenant_id == tenant_id,
                ImportMappingRow.key == key,
                ImportMappingRow.status == ImportMappingStatus.PUBLISHED.value,
            )
            .order_by(ImportMappingRow.version_number.desc())
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_to_domain(r) for r in rows]

    async def list_for_tenant(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> Sequence[ImportMapping]:
        """List all mapping versions for ``tenant_id``, newest first."""
        stmt = (
            select(ImportMappingRow)
            .where(ImportMappingRow.tenant_id == tenant_id)
            .order_by(ImportMappingRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_to_domain(r) for r in rows]

    async def get_published_for_key(self, tenant_id: UUID, key: str) -> ImportMapping | None:
        """Return the latest published mapping for ``key``, or None."""
        stmt = (
            select(ImportMappingRow)
            .where(
                ImportMappingRow.tenant_id == tenant_id,
                ImportMappingRow.key == key,
                ImportMappingRow.status == ImportMappingStatus.PUBLISHED.value,
            )
            .order_by(ImportMappingRow.version_number.desc())
            .limit(1)
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _to_domain(row) if row else None

    async def add(self, mapping: ImportMapping) -> None:
        """Stage a new mapping row; unique violations surface as Conflict."""
        now = mapping.created_at or datetime.now(tz=UTC)
        self._s.add(
            ImportMappingRow(
                id=mapping.id,
                tenant_id=mapping.tenant_id,
                key=mapping.key,
                version_number=mapping.version_number,
                mapping_json=dict(mapping.mapping_json),
                status=mapping.status.value,
                published_at=mapping.published_at,
                published_by=mapping.published_by,
                created_at=now,
                updated_at=now,
            )
        )
        try:
            await self._s.flush()
        except IntegrityError as exc:
            raise Conflict(
                f"import mapping {mapping.key} v{mapping.version_number} exists"
            ) from exc

    async def mark_published(
        self,
        tenant_id: UUID,
        mapping_id: UUID,
        *,
        published_at: datetime,
        published_by: UUID,
    ) -> None:
        """Transition a draft mapping to ``published``.

        Raises :class:`Conflict` if the mapping does not exist or is not
        in ``draft`` status (the domain ``publish()`` is the primary
        guard; this is the second-line check at the DB layer).
        """
        stmt = select(ImportMappingRow).where(
            ImportMappingRow.tenant_id == tenant_id,
            ImportMappingRow.id == mapping_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise Conflict(f"import mapping {mapping_id} not found")
        if row.status != ImportMappingStatus.DRAFT.value:
            raise Conflict(f"cannot publish a {row.status} import mapping (expected draft)")
        row.status = ImportMappingStatus.PUBLISHED.value
        row.published_at = published_at
        row.published_by = published_by

    async def mark_retired(
        self,
        tenant_id: UUID,
        mapping_id: UUID,
        *,
        actor_id: UUID,
        at: datetime,
    ) -> None:
        """Transition a published mapping to ``retired``.

        Raises :class:`Conflict` if the mapping does not exist or is not
        in ``published`` status.
        """
        stmt = select(ImportMappingRow).where(
            ImportMappingRow.tenant_id == tenant_id,
            ImportMappingRow.id == mapping_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise Conflict(f"import mapping {mapping_id} not found")
        if row.status != ImportMappingStatus.PUBLISHED.value:
            raise Conflict(f"cannot retire a {row.status} import mapping (expected published)")
        row.status = ImportMappingStatus.RETIRED.value


__all__ = ["SqlImportMappingRepository"]
