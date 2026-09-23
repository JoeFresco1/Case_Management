"""SQL composition provider for the forms HTTP boundary.

This adapter is deliberately outside ``app.interfaces``. It binds the local
SQLAlchemy repositories and transaction-bound stores to the infra-free
forms HTTP provider contract; an Azure adapter can implement the same
contract later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.forms.http_dependencies import FormsHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    SqlOutboxWriter,
)
from app.infrastructure.persistence.repositories import (
    SqlCaseRepository,
    SqlCaseTypeVersionRepository,
)
from app.infrastructure.persistence.repositories.form_repository import (
    SqlFormBindingRepository,
    SqlFormSessionRepository,
    SqlFormVersionRepository,
)


class SqlFormsHttpDependencyProvider:
    """Bind all forms HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> FormsHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return FormsHttpRepositories(
            case=SqlCaseRepository(db),
            case_type_version=SqlCaseTypeVersionRepository(db),
            form_binding=SqlFormBindingRepository(db),
            form_version=SqlFormVersionRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
            session=SqlFormSessionRepository(db),
        )


__all__ = ["SqlFormsHttpDependencyProvider"]
