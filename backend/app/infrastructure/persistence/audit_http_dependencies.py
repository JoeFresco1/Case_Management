"""SQL composition provider for the audit HTTP boundary.

This adapter binds transaction-scoped SQL repositories and the persistence
cursor/dedupe functions to the infra-free application contract. An Azure
provider can implement the same contract without changing the HTTP router.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit.http_dependencies import AuditHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    audit_dedupe,
    decode_timeline_cursor,
    encode_timeline_cursor,
)
from app.infrastructure.persistence.repositories import SqlCaseRepository


class SqlAuditHttpDependencyProvider:
    """Bind all audit HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> AuditHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        audit = SqlAuditEventStore(db)
        return AuditHttpRepositories(
            audit=audit,
            case=SqlCaseRepository(db),
            dedupe=audit_dedupe(db),
            cursor_encoder=encode_timeline_cursor,
            cursor_decoder=decode_timeline_cursor,
            correction_lookup=audit,
        )


__all__ = ["SqlAuditHttpDependencyProvider"]
