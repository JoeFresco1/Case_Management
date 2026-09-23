"""Dependency contracts for the connectors HTTP composition boundary.

The connectors router needs several ports bound to one request transaction.
This module describes that bundle without importing FastAPI, SQLAlchemy, or
any infrastructure adapter. The composition root supplies the concrete
provider.

Why no ``ConnectorRepository`` in the bundle: the first slice ships no SQL
adapter for connectors (per ADR-015 the production adapter is a separate
feature). The router keeps its local ``_build_connector_repo`` placeholder —
which imports nothing from ``app.infrastructure`` — until the SQL adapter
lands, at which point the port (``app.domain.ports.ConnectorRepository``)
is added to :class:`ConnectorHttpRepositories` without changing the router
shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import AuditEventStore, OutboxWriter, RoleRepository


@dataclass(frozen=True, slots=True)
class ConnectorHttpRepositories:
    """Ports required by the connectors HTTP endpoints for one session."""

    role: RoleRepository
    audit: AuditEventStore
    outbox: OutboxWriter


class ConnectorHttpDependencyProvider(Protocol):
    """Create a request-scoped connector port bundle from a persistence session."""

    def for_session(self, session: object) -> ConnectorHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = ["ConnectorHttpDependencyProvider", "ConnectorHttpRepositories"]
