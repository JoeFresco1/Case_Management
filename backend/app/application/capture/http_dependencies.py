"""Dependency contracts for the capture HTTP composition boundary.

The capture router (email + scan) needs several ports bound to one request
transaction. This module describes that bundle without importing FastAPI,
SQLAlchemy, or any infrastructure adapter. The composition root supplies
the concrete provider.

Why ``DocumentStore`` is NOT in the bundle: the store is request-independent
(it is not bound to the DB session), so the router resolves it from
``request.app.state.document_store`` via a local getter — the same pattern
as ``app.interfaces.http.routers.documents.get_document_store``. Likewise
the scan ``ScanAuthorizationProvider`` is a settings-backed adapter built
locally in the router (``app.config`` is not infrastructure), not a
session-bound port.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import (
    AuditEventStore,
    CapturedEmailRepository,
    CaseRepository,
    ChannelRepository,
    DocumentRepository,
    OutboxWriter,
    QuarantineRepository,
)


@dataclass(frozen=True, slots=True)
class CaptureHttpRepositories:
    """Ports required by the capture HTTP endpoints for one session."""

    channel: ChannelRepository
    case: CaseRepository
    document: DocumentRepository
    quarantine: QuarantineRepository
    captured_email: CapturedEmailRepository
    audit: AuditEventStore
    outbox: OutboxWriter


class CaptureHttpDependencyProvider(Protocol):
    """Create a request-scoped capture port bundle from a persistence session."""

    def for_session(self, session: object) -> CaptureHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = ["CaptureHttpDependencyProvider", "CaptureHttpRepositories"]
