"""SQL composition provider for the retention HTTP boundary.

This adapter is deliberately outside ``app.interfaces``. It binds the local
SQLAlchemy session-bound stores to the infra-free retention HTTP provider
contract; an Azure adapter can implement the same contract later.

The retention stores themselves (policy, disposition, archival, freeze,
orphan-proposal, and the shared legal-hold store) are process-scoped and
are resolved from ``app.state`` via the domain ports in
``app.domain.ports`` — they are not part of this per-session bundle. Both
the retention router and the retention-evidence router share this provider.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.retention.http_dependencies import RetentionHttpRepositories
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore
from app.infrastructure.persistence.repositories import SqlCaseRepository


class SqlRetentionHttpDependencyProvider:
    """Bind the retention session ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> RetentionHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return RetentionHttpRepositories(
            case=SqlCaseRepository(db),
            audit=SqlAuditEventStore(db),
        )


__all__ = ["SqlRetentionHttpDependencyProvider"]
