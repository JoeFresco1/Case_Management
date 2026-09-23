"""SQL composition provider for the case-relationship HTTP boundary.

This adapter is deliberately outside ``app.interfaces``. It binds the local
SQLAlchemy repositories and transaction-bound stores to the infra-free
case-relationship HTTP provider contract; an Azure adapter can implement
the same contract later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cases.relationship_http_dependencies import (
    CaseRelationshipHttpRepositories,
)
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    SqlOutboxWriter,
)
from app.infrastructure.persistence.repositories import (
    SqlCaseRelationshipRepository,
    SqlCaseRepository,
    SqlCaseTypeRepository,
    SqlCaseTypeVersionRepository,
    SqlDocumentRepository,
)


class SqlCaseRelationshipHttpDependencyProvider:
    """Bind all case-relationship HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> CaseRelationshipHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return CaseRelationshipHttpRepositories(
            case=SqlCaseRepository(db),
            case_type=SqlCaseTypeRepository(db),
            case_type_version=SqlCaseTypeVersionRepository(db),
            document=SqlDocumentRepository(db),
            relationship=SqlCaseRelationshipRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
        )


__all__ = ["SqlCaseRelationshipHttpDependencyProvider"]
