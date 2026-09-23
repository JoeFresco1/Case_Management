"""SQL composition provider for the legal-hold HTTP boundary.

This adapter is deliberately outside ``app.interfaces``. It binds the local
SQLAlchemy session-bound stores to the infra-free legal-hold HTTP provider
contract; an Azure adapter can implement the same contract later.

The legal-hold repository itself is process-scoped (shared across
requests) and is resolved from ``app.state`` via the ``LegalHoldRepository``
domain port — it is not part of this per-session bundle.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.holds.http_dependencies import HoldHttpRepositories
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore
from app.infrastructure.persistence.repositories import SqlCaseRepository


class SqlHoldHttpDependencyProvider:
    """Bind the legal-hold session ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> HoldHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return HoldHttpRepositories(
            case=SqlCaseRepository(db),
            audit=SqlAuditEventStore(db),
        )


__all__ = ["SqlHoldHttpDependencyProvider"]
