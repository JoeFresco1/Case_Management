"""SQL adapter for tenant-scoped participant persistence."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.participant import Participant
from app.infrastructure.persistence.mapping import participant_from_domain, participant_to_domain
from app.infrastructure.persistence.models import Participant as ParticipantRow


class SqlParticipantRepository:
    """Persist participants while retaining logical removal history."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, participant: Participant) -> None:
        """Stage a participant insert in the caller's transaction."""
        self._session.add(ParticipantRow(**participant_from_domain(participant)))

    async def get(self, tenant_id: UUID, participant_id: UUID) -> Participant | None:
        """Read one participant with explicit tenant isolation."""
        stmt = select(ParticipantRow).where(
            ParticipantRow.tenant_id == tenant_id, ParticipantRow.id == participant_id
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return participant_to_domain(row) if row else None

    async def list_for_case(self, tenant_id: UUID, case_id: UUID) -> Sequence[Participant]:
        """Read all current and removed participants for a case."""
        stmt = (
            select(ParticipantRow)
            .where(ParticipantRow.tenant_id == tenant_id, ParticipantRow.case_id == case_id)
            .order_by(ParticipantRow.added_at)
        )
        return [participant_to_domain(row) for row in (await self._session.execute(stmt)).scalars()]

    async def update(self, participant: Participant) -> None:
        """Update participant metadata without deleting its identity."""
        stmt = (
            update(ParticipantRow)
            .where(
                ParticipantRow.tenant_id == participant.tenant_id,
                ParticipantRow.id == participant.id,
            )
            .values(**participant_from_domain(participant))
        )
        await self._session.execute(stmt)


__all__ = ["SqlParticipantRepository"]
