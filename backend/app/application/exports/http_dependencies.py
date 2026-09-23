"""Dependency contracts for the evidence-bundle export HTTP boundary.

The exports router (``app.interfaces.http.routers.exports``) needs
several ports bound to one request transaction. This module describes
that bundle without importing FastAPI, SQLAlchemy, or any
infrastructure adapter. The composition root supplies the concrete
provider.

The ``DocumentStore`` port is intentionally NOT part of this bundle:
the router resolves it from ``request.app.state.document_store``
following the documents router pattern
(``app.interfaces.http.routers.documents.get_document_store``), which
keeps the store's process-wide lifecycle separate from the
per-request SQL session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.audit.record_event import Dedupe
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
)


@dataclass(frozen=True, slots=True)
class EvidenceBundleHttpRepositories:
    """Ports required by the evidence-bundle export endpoint for one session."""

    case: CaseRepository
    case_type_version: CaseTypeVersionRepository
    document: DocumentRepository
    audit: AuditEventStore
    dedupe: Dedupe


class EvidenceBundleHttpDependencyProvider(Protocol):
    """Create a request-scoped evidence-bundle port bundle from a session."""

    def for_session(self, session: object) -> EvidenceBundleHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "EvidenceBundleHttpDependencyProvider",
    "EvidenceBundleHttpRepositories",
]
