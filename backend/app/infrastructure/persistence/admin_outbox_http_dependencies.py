"""SQL composition provider for the admin outbox HTTP boundary.

This adapter stays outside ``app.interfaces``. It binds the local
SQLAlchemy role repository to the infra-free admin outbox provider
contract; an Azure adapter can implement the same contract later.

The outbox dispatcher itself is application-scoped (it owns its own
session factory) and is resolved from ``request.app.state`` by the
router through the ``OutboxAdminService`` Protocol — it is not part
of this per-session bundle.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outbox.http_dependencies import AdminOutboxHttpRepositories
from app.infrastructure.persistence.repositories import SqlRoleRepository


class SqlAdminOutboxHttpDependencyProvider:
    """Bind the admin outbox HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> AdminOutboxHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return AdminOutboxHttpRepositories(
            role=SqlRoleRepository(db),
        )


__all__ = ["SqlAdminOutboxHttpDependencyProvider"]
