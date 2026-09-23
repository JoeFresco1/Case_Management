"""SQL composition provider for the migration / coexistence HTTP boundary.

This adapter stays outside ``app.interfaces``. It binds the local
SQLAlchemy role repository and the transaction-bound audit / outbox
writers to the infra-free migration provider contract; an Azure
adapter can implement the same contract later.

The coexistence policy persistence itself is still a router-local
in-memory placeholder (no SQL adapter yet); it is not part of this
bundle.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.migration.http_dependencies import MigrationHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    SqlOutboxWriter,
)
from app.infrastructure.persistence.repositories import SqlRoleRepository


class SqlMigrationHttpDependencyProvider:
    """Bind the migration HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> MigrationHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return MigrationHttpRepositories(
            role=SqlRoleRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
        )


__all__ = ["SqlMigrationHttpDependencyProvider"]
