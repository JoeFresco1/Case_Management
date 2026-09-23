"""Dependency contracts for the operational-reports HTTP boundary.

The reports router (``app.interfaces.http.routers.reports``) needs
several ports bound to one request transaction. This module describes
that bundle without importing FastAPI, SQLAlchemy, or any
infrastructure adapter. The composition root supplies the concrete
provider.

The custom-report definition repository
(``app.application.reports.custom_reports.ReportDefinitionRepository``)
is intentionally NOT part of this bundle: it is an application-layer
in-memory store (not an infrastructure adapter), so the router keeps
calling ``custom_reports.default_repository()`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.audit.record_event import Dedupe
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    QueueRepository,
    TaskRepository,
)


@dataclass(frozen=True, slots=True)
class ReportsHttpRepositories:
    """Ports required by the operational-reports HTTP endpoints for one session."""

    case: CaseRepository
    task: TaskRepository
    queue: QueueRepository
    audit: AuditEventStore
    dedupe: Dedupe


class ReportsHttpDependencyProvider(Protocol):
    """Create a request-scoped reports port bundle from a session."""

    def for_session(self, session: object) -> ReportsHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "ReportsHttpDependencyProvider",
    "ReportsHttpRepositories",
]
