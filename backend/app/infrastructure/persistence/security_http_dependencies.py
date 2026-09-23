"""SQL composition provider for the security HTTP boundary.

This adapter stays outside ``app.interfaces``. It binds the
transaction-bound SQL audit store (so session-control and
break-glass events land on the same hash chain as everything else)
and the durable break-glass grant store to the infra-free security
provider contract; an Azure adapter can implement the same contract
later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.security.http_dependencies import SecurityHttpRepositories
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore
from app.infrastructure.persistence.repositories import (
    SqlBreakGlassGrantRepository,
)


class SqlSecurityHttpDependencyProvider:
    """Bind the security HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> SecurityHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return SecurityHttpRepositories(
            audit=SqlAuditEventStore(db),
            grant=SqlBreakGlassGrantRepository(db),
        )


__all__ = ["SqlSecurityHttpDependencyProvider"]
