"""SQL composition provider for the channels/quarantine HTTP boundary.

This adapter is deliberately outside ``app.interfaces``. It binds the local
SQLAlchemy repositories and transaction-bound stores to the infra-free
channels HTTP provider contract; an Azure adapter can implement the same
contract later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.channels.http_dependencies import ChannelHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    SqlOutboxWriter,
)
from app.infrastructure.persistence.repositories import (
    SqlChannelRepository,
    SqlQuarantineRepository,
    SqlRoleRepository,
)


class SqlChannelHttpDependencyProvider:
    """Bind all channels/quarantine HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> ChannelHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return ChannelHttpRepositories(
            channel=SqlChannelRepository(db),
            quarantine=SqlQuarantineRepository(db),
            role=SqlRoleRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
        )


__all__ = ["SqlChannelHttpDependencyProvider"]
