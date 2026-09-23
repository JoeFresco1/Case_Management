"""Dependency contracts for the queue HTTP composition boundary (FM-016).

The queue router needs queue + task + case-lookup persistence plus audit
and outbox ports bound to one request transaction. This module describes
that bundle without importing FastAPI, SQLAlchemy, or any infrastructure
adapter. The composition root supplies the concrete provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    OutboxWriter,
    QueueRepository,
    RoleRepository,
    TaskRepository,
)


@dataclass(frozen=True, slots=True)
class QueueHttpRepositories:
    """Ports required by the queue HTTP endpoints for one session."""

    queue: QueueRepository
    task: TaskRepository
    case: CaseRepository
    role: RoleRepository
    audit: AuditEventStore
    outbox: OutboxWriter


class QueueHttpDependencyProvider(Protocol):
    """Create a request-scoped queue port bundle from a session."""

    def for_session(self, session: object) -> QueueHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "QueueHttpDependencyProvider",
    "QueueHttpRepositories",
]
