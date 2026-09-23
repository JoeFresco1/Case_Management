"""Dependency contract for the worker-home HTTP boundary (T5 worker UX).

The router needs the process-local dashboard/preference/policy stores
plus audit and outbox bound to one request transaction. This module
describes that bundle without importing FastAPI, SQLAlchemy, or any
infrastructure adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.worker_home.worker_home import (
    DashboardStore,
    PreferencePolicyStore,
    PreferenceStore,
)
from app.domain.ports import AuditEventStore, OutboxWriter


@dataclass(frozen=True, slots=True)
class WorkerHomeHttpRepositories:
    """Ports required by the worker-home HTTP endpoints for one session."""

    dashboards: DashboardStore
    preferences: PreferenceStore
    policies: PreferencePolicyStore
    audit: AuditEventStore
    outbox: OutboxWriter


class WorkerHomeHttpDependencyProvider(Protocol):
    """Create a request-scoped worker-home port bundle from a session."""

    def for_session(self, session: object) -> WorkerHomeHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = ["WorkerHomeHttpDependencyProvider", "WorkerHomeHttpRepositories"]
