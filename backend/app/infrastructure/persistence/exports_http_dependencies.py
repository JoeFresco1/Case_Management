"""SQL composition provider for the evidence-bundle export HTTP boundary.

This adapter is deliberately outside ``app.interfaces``. It binds the
local SQLAlchemy repositories and the transaction-bound audit/dedupe
helpers to the infra-free exports HTTP provider contract; an Azure
adapter can implement the same contract later.

The ``DocumentStore`` is intentionally NOT part of this bundle: the
router resolves the process-wide store from ``request.app.state``
following the documents router pattern.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.exports.http_dependencies import EvidenceBundleHttpRepositories
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    audit_dedupe,
)
from app.infrastructure.persistence.repositories import (
    SqlCaseRepository,
    SqlCaseTypeVersionRepository,
    SqlDocumentRepository,
)


class SqlEvidenceBundleHttpDependencyProvider:
    """Bind all evidence-bundle export HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> EvidenceBundleHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return EvidenceBundleHttpRepositories(
            case=SqlCaseRepository(db),
            case_type_version=SqlCaseTypeVersionRepository(db),
            document=SqlDocumentRepository(db),
            audit=SqlAuditEventStore(db),
            dedupe=audit_dedupe(db),
        )


__all__ = ["SqlEvidenceBundleHttpDependencyProvider"]
