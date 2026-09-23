"""SQL composition provider for the queue HTTP boundary (FM-016).

This adapter stays outside ``app.interfaces``. It binds the local SQLAlchemy
queue/task/case/role repositories plus audit and outbox stores to the
infra-free queue provider contract; an Azure adapter can implement the same
contract later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.queues.http_dependencies import QueueHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    SqlOutboxWriter,
)
from app.infrastructure.persistence.repositories import (
    SqlCaseRepository,
    SqlQueueRepository,
    SqlRoleRepository,
    SqlTaskRepository,
)


class SqlQueueHttpDependencyProvider:
    """Bind all queue HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> QueueHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return QueueHttpRepositories(
            queue=SqlQueueRepository(db),
            task=SqlTaskRepository(db),
            case=SqlCaseRepository(db),
            role=SqlRoleRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
        )


__all__ = ["SqlQueueHttpDependencyProvider"]
