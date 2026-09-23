"""SQL adapter for :class:`app.domain.saved_search.SavedSearch` (P06-US3 / FM-032).

Tenant isolation: every query includes ``WHERE tenant_id = :tenant_id``.
The unique constraint on ``(tenant_id, owner_user_id, name)`` is the
duplicate-name guard — a re-save under the same name raises
``IntegrityError`` which the application layer translates to
``Conflict`` (the router maps to 409).

The same module also implements :class:`app.domain.ports.SavedSearchAuditRepository`
so the save + run + audit transactions share one ``AsyncSession`` and
commit once at the boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import Conflict
from app.domain.saved_search import SavedSearch, SortSpec
from app.infrastructure.persistence.models import (
    SavedSearch as SavedSearchRow,
)
from app.infrastructure.persistence.models import (
    SearchAudit as SearchAuditRow,
)


def _row_to_domain(row: SavedSearchRow) -> SavedSearch:
    """Project the ORM row onto the domain aggregate.

    The ``filters`` tuple is rebuilt from the JSON column so the
    application layer sees the same shape it sent. ``sort`` entries
    are reconstructed through :class:`SortSpec.from_dict` so the
    domain invariant runs again on read (defense-in-depth: a malformed
    JSON column surfaces as ``InvariantViolation`` rather than
    crashing a downstream consumer).
    """
    sort = tuple(
        SortSpec.from_dict(item) for item in (row.sort_json or []) if isinstance(item, dict)
    )
    filters = tuple(
        (item.get("field", ""), item.get("op", "eq"), item.get("value"))
        for item in (row.filters_json or [])
        if isinstance(item, dict)
    )
    return SavedSearch(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        text_query=row.text_query,
        filters=filters,
        sort=sort,
        query_shape=dict(row.query_shape or {}),
        created_at=row.created_at,
    )


class SqlSavedSearchRepository:
    """SQL-backed ``SavedSearchRepository`` (P06-US3 / FM-032)."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, saved: SavedSearch) -> None:
        row = SavedSearchRow(
            id=saved.id,
            tenant_id=saved.tenant_id,
            owner_user_id=saved.owner_user_id,
            name=saved.name,
            text_query=saved.text_query,
            filters_json=[{"field": f[0], "op": f[1], "value": f[2]} for f in saved.filters],
            sort_json=[s.to_dict() for s in saved.sort],
            query_shape=dict(saved.query_shape),
            created_at=saved.created_at,
        )
        self._s.add(row)
        try:
            await self._s.flush()
        except IntegrityError as exc:
            # Duplicate (tenant, owner, name) — the unique constraint
            # fired. The router translates Conflict to 409 so the
            # worker can rename their saved search and retry.
            raise Conflict(f"saved search with name {saved.name!r} already exists") from exc

    async def get(self, tenant_id: UUID, saved_search_id: UUID) -> SavedSearch | None:
        stmt = select(SavedSearchRow).where(
            SavedSearchRow.tenant_id == tenant_id,
            SavedSearchRow.id == saved_search_id,
        )
        row = (await self._s.execute(stmt)).scalar_one_or_none()
        return _row_to_domain(row) if row else None

    async def list_for_owner(
        self,
        tenant_id: UUID,
        owner_user_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[SavedSearch]:
        stmt = (
            select(SavedSearchRow)
            .where(SavedSearchRow.tenant_id == tenant_id)
            .where(SavedSearchRow.owner_user_id == owner_user_id)
            .order_by(SavedSearchRow.created_at.desc(), SavedSearchRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_row_to_domain(r) for r in rows]

    async def delete(self, tenant_id: UUID, saved_search_id: UUID) -> bool:
        row = await self.get(tenant_id, saved_search_id)
        if row is None:
            return False
        # Refetch the ORM row by id so we can delete through the
        # session. The earlier ``get`` already projected the row into
        # the domain aggregate; we delete via a fresh PK lookup so we
        # operate on the session's identity map.
        stmt = select(SavedSearchRow).where(
            SavedSearchRow.tenant_id == tenant_id,
            SavedSearchRow.id == saved_search_id,
        )
        db_row = (await self._s.execute(stmt)).scalar_one_or_none()
        if db_row is None:
            return False
        await self._s.delete(db_row)
        await self._s.flush()
        return True


class SqlSavedSearchAuditRepository:
    """SQL-backed ``SavedSearchAuditRepository`` (P06-US6 / FM-032).

    Append-only by design: the application layer never calls
    ``update`` or ``delete``; the table has no UPDATE/DELETE paths in
    the adapter. A future correction would land as a compensating
    ``audit_events`` row referencing this row's id (Constitution IV).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def append(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID,
        saved_search_id: UUID | None,
        query_shape: dict[str, Any],
        result_count: int,
        occurred_at: datetime,
    ) -> None:
        self._s.add(
            SearchAuditRow(
                tenant_id=tenant_id,
                actor_id=actor_id,
                saved_search_id=saved_search_id,
                query_shape=dict(query_shape),
                result_count=int(result_count),
                occurred_at=occurred_at,
            )
        )
        await self._s.flush()

    async def list_for_security_report(
        self,
        *,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        saved_search_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[SearchAuditRow]:
        stmt = select(SearchAuditRow).where(SearchAuditRow.tenant_id == tenant_id)
        if actor_id is not None:
            stmt = stmt.where(SearchAuditRow.actor_id == actor_id)
        if saved_search_id is not None:
            stmt = stmt.where(SearchAuditRow.saved_search_id == saved_search_id)
        if since is not None:
            stmt = stmt.where(SearchAuditRow.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(SearchAuditRow.occurred_at <= until)
        stmt = (
            stmt.order_by(SearchAuditRow.occurred_at.asc(), SearchAuditRow.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return (await self._s.execute(stmt)).scalars().all()


__all__ = [
    "SqlSavedSearchAuditRepository",
    "SqlSavedSearchRepository",
]
