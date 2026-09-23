"""SQL composition provider for the admin form-binding HTTP boundary (P07 / FM-036).

This adapter stays outside ``app.interfaces``. It binds the local
SQLAlchemy repositories and audit store to the infra-free admin
form-binding provider contract; an Azure adapter can implement the
same contract later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.forms.admin.http_dependencies import (
    AdminFormBindingsHttpRepositories,
)
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
)
from app.infrastructure.persistence.repositories import SqlRoleRepository
from app.infrastructure.persistence.repositories.form_repository import (
    SqlFormBindingRepository,
)


class SqlAdminFormBindingsHttpDependencyProvider:
    """Bind all admin form-binding HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> AdminFormBindingsHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return AdminFormBindingsHttpRepositories(
            form_binding=SqlFormBindingRepository(db),
            role=SqlRoleRepository(db),
            audit=SqlAuditEventStore(db),
        )


__all__ = ["SqlAdminFormBindingsHttpDependencyProvider"]
