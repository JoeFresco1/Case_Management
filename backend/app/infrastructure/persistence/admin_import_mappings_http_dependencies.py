"""SQL composition provider for the admin import-mapping HTTP boundary (FM-026).

This adapter stays outside ``app.interfaces``. It binds the local
SQLAlchemy repositories and audit store to the infra-free admin
import-mapping provider contract; an Azure adapter can implement the
same contract later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.imports.admin.http_dependencies import (
    AdminImportMappingsHttpRepositories,
)
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore
from app.infrastructure.persistence.repositories import (
    SqlImportMappingRepository,
    SqlRoleRepository,
)


class SqlAdminImportMappingsHttpDependencyProvider:
    """Bind all admin import-mapping HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> AdminImportMappingsHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return AdminImportMappingsHttpRepositories(
            import_mapping=SqlImportMappingRepository(db),
            audit=SqlAuditEventStore(db),
            role=SqlRoleRepository(db),
        )


__all__ = ["SqlAdminImportMappingsHttpDependencyProvider"]
