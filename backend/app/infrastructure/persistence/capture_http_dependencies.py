"""SQL composition provider for the capture HTTP boundary.

This adapter is deliberately outside ``app.interfaces``. It binds the local
SQLAlchemy repositories and transaction-bound stores to the infra-free
capture HTTP provider contract; an Azure adapter can implement the same
contract later.

Why no document store here: the store is request-independent (not bound to
the DB session), so the router resolves it from ``request.app.state``
— the same pattern as the documents router.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.capture.http_dependencies import CaptureHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    SqlOutboxWriter,
)
from app.infrastructure.persistence.repositories import (
    SqlCapturedEmailRepository,
    SqlCaseRepository,
    SqlChannelRepository,
    SqlDocumentRepository,
    SqlQuarantineRepository,
)


class SqlCaptureHttpDependencyProvider:
    """Bind all capture HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> CaptureHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return CaptureHttpRepositories(
            channel=SqlChannelRepository(db),
            case=SqlCaseRepository(db),
            document=SqlDocumentRepository(db),
            quarantine=SqlQuarantineRepository(db),
            captured_email=SqlCapturedEmailRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
        )


__all__ = ["SqlCaptureHttpDependencyProvider"]
