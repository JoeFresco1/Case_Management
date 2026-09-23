"""SQL composition provider for the admin role HTTP boundary.

This adapter stays outside ``app.interfaces``. It binds the local SQLAlchemy
role repository and audit store to the infra-free admin role provider
contract; an Azure adapter can implement the same contract later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.roles.http_dependencies import AdminRolesHttpRepositories
from app.infrastructure.persistence.audit_outbox import SqlAuditEventStore
from app.infrastructure.persistence.repositories import SqlRoleRepository


class SqlAdminRolesHttpDependencyProvider:
    """Bind the admin role HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> AdminRolesHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return AdminRolesHttpRepositories(
            role=SqlRoleRepository(db),
            audit=SqlAuditEventStore(db),
        )


__all__ = ["SqlAdminRolesHttpDependencyProvider"]
