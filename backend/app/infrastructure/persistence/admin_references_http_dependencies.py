"""SQL composition provider for the admin reference-format HTTP boundary.

Both ports are bound to the request's SQLAlchemy session: the audit
store and the durable ``reference_formats`` table (FM-009). Overrides
survive restarts and are visible to ``SqlCaseRepository.next_reference``
in the same transaction they are configured in.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.references.http_dependencies import (
    AdminReferencesHttpRepositories,
)
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore
from app.infrastructure.persistence.repositories import (
    SqlReferenceFormatRepository,
)


class SqlAdminReferencesHttpDependencyProvider:
    """Bind the admin reference-format ports to the local SQL boundary."""

    def for_session(self, session: object) -> AdminReferencesHttpRepositories:
        """Create a bundle whose adapters share the request transaction."""
        db = cast(AsyncSession, session)
        return AdminReferencesHttpRepositories(
            reference_format=SqlReferenceFormatRepository(db),
            audit=SqlAuditEventStore(db),
        )


__all__ = ["SqlAdminReferencesHttpDependencyProvider"]
