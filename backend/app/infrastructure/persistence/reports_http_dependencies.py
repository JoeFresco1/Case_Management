"""SQL composition provider for the operational-reports HTTP boundary.

This adapter is deliberately outside ``app.interfaces``. It binds the
local SQLAlchemy repositories and the transaction-bound audit/dedupe
helpers to the infra-free reports HTTP provider contract; an Azure
adapter can implement the same contract later.

The custom-report definition repository is intentionally NOT part of
this bundle: it is an application-layer in-memory store
(``app.application.reports.custom_reports.ReportDefinitionRepository``),
not an infrastructure adapter, so the router keeps resolving it
directly.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.reports.http_dependencies import ReportsHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    audit_dedupe,
)
from app.infrastructure.persistence.repositories import (
    SqlCaseRepository,
    SqlQueueRepository,
    SqlTaskRepository,
)


class SqlReportsHttpDependencyProvider:
    """Bind all operational-reports HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> ReportsHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return ReportsHttpRepositories(
            case=SqlCaseRepository(db),
            task=SqlTaskRepository(db),
            queue=SqlQueueRepository(db),
            audit=SqlAuditEventStore(db),
            dedupe=audit_dedupe(db),
        )


__all__ = ["SqlReportsHttpDependencyProvider"]
