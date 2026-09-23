"""SQL repository for the ``configuration_publications`` table (FM-021).

The repository is the local adapter behind the
:class:`ConfigurationPublicationRepository` Protocol defined in
``app.domain.ports``. It implements the four methods the
``governance`` use cases need:

- :meth:`SqlConfigurationPublicationRepository.add` — append a new
  row (called from the publish / retire / approve flows).
- :meth:`SqlConfigurationPublicationRepository.list_for_artifact` —
  the per-artifact history (the admin timeline UI).
- :meth:`SqlConfigurationPublicationRepository.list_tenant_history` —
  the tenant-wide timeline (the "all activity" view).

Tenant isolation is enforced at every query
(``WHERE tenant_id = :tenant_id``). The table is append-only — the
repository never issues UPDATE / DELETE — matching the
:attr:`ConfigurationPublication` invariant documented in
``models.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.configuration.governance import PublicationRecord
from app.infrastructure.persistence.models import ConfigurationPublication as Row


def _now_utc() -> datetime:
    """Return a tz-aware UTC ``datetime`` for ``occurred_at``/``created_at``."""
    return datetime.now(tz=UTC)


class SqlConfigurationPublicationRepository:
    """The SQL adapter for the configuration publications table (FM-021).

    All methods take ``tenant_id`` as the first argument so the
    SQLAlchemy queries can include ``WHERE tenant_id = :tenant_id``
    (AGENTS.md §3 R1 / §12). A missing tenant scope is a programmer
    error; the query simply returns nothing if a caller forgets to
    scope the call.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(
        self,
        *,
        tenant_id: UUID,
        artifact_type: str,
        artifact_id: UUID,
        version_id: UUID,
        action: str,
        actor_id: UUID,
        change_summary: str,
        occurred_at: datetime,
    ) -> UUID:
        row = Row(
            tenant_id=tenant_id,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            version_id=version_id,
            action=action,
            actor_id=actor_id,
            change_summary=change_summary,
            occurred_at=occurred_at,
            created_at=_now_utc(),
        )
        self._s.add(row)
        await self._s.flush()
        return row.id

    async def list_for_artifact(
        self,
        tenant_id: UUID,
        artifact_type: str,
        artifact_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[PublicationRecord]:
        stmt = (
            select(Row)
            .where(
                Row.tenant_id == tenant_id,
                Row.artifact_type == artifact_type,
                Row.artifact_id == artifact_id,
            )
            .order_by(Row.occurred_at.desc(), Row.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_to_record(r) for r in rows]

    async def list_tenant_history(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        artifact_type: str | None = None,
    ) -> Sequence[PublicationRecord]:
        stmt = select(Row).where(Row.tenant_id == tenant_id)
        if artifact_type is not None:
            stmt = stmt.where(Row.artifact_type == artifact_type)
        stmt = stmt.order_by(Row.occurred_at.desc(), Row.id.desc()).limit(limit).offset(offset)
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_to_record(r) for r in rows]


def _to_record(row: Row) -> PublicationRecord:
    return PublicationRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        artifact_type=row.artifact_type,
        artifact_id=row.artifact_id,
        version_id=row.version_id,
        action=row.action,
        actor_id=row.actor_id,
        occurred_at=row.occurred_at,
        change_summary=row.change_summary or "",
    )


__all__ = ["SqlConfigurationPublicationRepository"]
