"""Dependency contracts for the migration / coexistence HTTP composition boundary.

The coexistence router needs the role repository (for the
``ADMIN_TENANT_CONFIG`` authorization check) plus the audit and
outbox writers bound to one request transaction. This module
describes that bundle without importing FastAPI, SQLAlchemy, or any
infrastructure adapter. The composition root supplies the concrete
provider.

The coexistence policy persistence itself stays router-local for now:
the first slice ships an in-memory placeholder (no SQL adapter yet)
and the use cases accept it as ``Any``. When the SQL adapter lands,
its port is ``app.domain.ports.CoexistencePolicyRepository`` and the
provider bundle gains a ``policy`` field without changing the router
call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import AuditEventStore, OutboxWriter, RoleRepository


@dataclass(frozen=True, slots=True)
class MigrationHttpRepositories:
    """Ports required by the coexistence endpoints for one session."""

    role: RoleRepository
    audit: AuditEventStore
    outbox: OutboxWriter


class MigrationHttpDependencyProvider(Protocol):
    """Create a request-scoped migration port bundle from a persistence session."""

    def for_session(self, session: object) -> MigrationHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "MigrationHttpDependencyProvider",
    "MigrationHttpRepositories",
]
