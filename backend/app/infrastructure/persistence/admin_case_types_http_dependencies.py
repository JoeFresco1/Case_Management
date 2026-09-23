"""SQL composition provider for the admin CaseType HTTP boundary.

This adapter stays outside ``app.interfaces``. It binds the local SQLAlchemy
repositories and audit store to the infra-free admin CaseType provider
contract; an Azure adapter can implement the same contract later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.configuration.http_dependencies import (
    AdminCaseTypesHttpRepositories,
)
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore
from app.infrastructure.persistence.repositories import (
    SqlCaseTypeRepository,
    SqlCaseTypeVersionRepository,
    SqlRoleRepository,
)


class SqlAdminCaseTypesHttpDependencyProvider:
    """Bind all admin CaseType HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> AdminCaseTypesHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return AdminCaseTypesHttpRepositories(
            case_type=SqlCaseTypeRepository(db),
            case_type_version=SqlCaseTypeVersionRepository(db),
            audit=SqlAuditEventStore(db),
            role=SqlRoleRepository(db),
        )


__all__ = ["SqlAdminCaseTypesHttpDependencyProvider"]
