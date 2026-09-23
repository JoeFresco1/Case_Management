"""Dependency contracts for the viewer HTTP composition boundary (T5 / FM-023 ext).

The viewer router needs the document aggregate plus the three viewer
ports (annotations, checkouts, renditions) plus audit and outbox bound
to one request transaction. This module describes that bundle without
importing FastAPI, SQLAlchemy, or any infrastructure adapter. The
composition root supplies the concrete provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import (
    AnnotationRepository,
    AuditEventStore,
    CheckoutLockRepository,
    DocumentRepository,
    OutboxWriter,
    RenditionRepository,
)


@dataclass(frozen=True, slots=True)
class ViewerHttpRepositories:
    """Ports required by the viewer HTTP endpoints for one session.

    The ``DocumentStore`` (bytes) is resolved separately from
    ``app.state`` via the shared ``get_document_store`` dependency —
    the same seam the upload/version/redaction endpoints use — so the
    byte adapter stays swappable without touching this bundle.
    """

    document: DocumentRepository
    annotation: AnnotationRepository
    checkout: CheckoutLockRepository
    rendition: RenditionRepository
    audit: AuditEventStore
    outbox: OutboxWriter


class ViewerHttpDependencyProvider(Protocol):
    """Create a request-scoped viewer port bundle from a session."""

    def for_session(self, session: object) -> ViewerHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "ViewerHttpDependencyProvider",
    "ViewerHttpRepositories",
]
