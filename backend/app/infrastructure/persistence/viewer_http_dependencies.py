"""SQL composition provider for the viewer HTTP boundary (T5 / FM-023 ext).

This adapter stays outside ``app.interfaces``. It binds the local
SQLAlchemy document + viewer repositories plus audit and outbox stores
to the infra-free viewer provider contract; an Azure adapter can
implement the same contract later.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.documents.http_dependencies import ViewerHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    SqlOutboxWriter,
)
from app.infrastructure.persistence.documents import SqlDocumentRepository
from app.infrastructure.persistence.viewer_repository import (
    SqlAnnotationRepository,
    SqlCheckoutLockRepository,
    SqlRenditionRepository,
)


class SqlViewerHttpDependencyProvider:
    """Bind all viewer HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> ViewerHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return ViewerHttpRepositories(
            document=SqlDocumentRepository(db),
            annotation=SqlAnnotationRepository(db),
            checkout=SqlCheckoutLockRepository(db),
            rendition=SqlRenditionRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
        )


__all__ = ["SqlViewerHttpDependencyProvider"]
