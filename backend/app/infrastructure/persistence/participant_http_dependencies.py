"""SQL composition provider for the participant HTTP boundary."""

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.participants.http_dependencies import ParticipantHttpRepositories
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore, SqlOutboxWriter
from app.infrastructure.persistence.repositories import SqlCaseRepository, SqlParticipantRepository


class SqlParticipantHttpDependencyProvider:
    """Bind participant ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> ParticipantHttpRepositories:
        """Create adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return ParticipantHttpRepositories(
            case=SqlCaseRepository(db),
            participant=SqlParticipantRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
        )


__all__ = ["SqlParticipantHttpDependencyProvider"]
