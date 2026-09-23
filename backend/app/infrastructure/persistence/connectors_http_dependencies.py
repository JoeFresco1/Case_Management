"""SQL composition provider for the connectors HTTP boundary.

This adapter is deliberately outside ``app.interfaces``. It binds the local
SQLAlchemy repositories and transaction-bound stores to the infra-free
connectors HTTP provider contract; an Azure adapter can implement the same
contract later.

Why no connector repository here: the first slice ships no SQL adapter for
connectors (per ADR-015 the production adapter is a separate feature). The
router keeps its local placeholder until the SQL adapter lands, at which
point it joins this bundle without changing the router shape.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.connectors.http_dependencies import ConnectorHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    SqlOutboxWriter,
)
from app.infrastructure.persistence.repositories import SqlRoleRepository


class SqlConnectorHttpDependencyProvider:
    """Bind all connectors HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> ConnectorHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return ConnectorHttpRepositories(
            role=SqlRoleRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
        )


__all__ = ["SqlConnectorHttpDependencyProvider"]
