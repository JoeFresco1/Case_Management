"""Dependency contracts for the security HTTP composition boundary.

The session-control and break-glass endpoints append to the shared
hash-chained audit store, so they need an ``AuditEventStore`` bound to
the request transaction. The break-glass grant lifecycle and the
read-side enforcement hook need the durable grant store in the same
transaction. This module describes that bundle without importing
FastAPI, SQLAlchemy, or any infrastructure adapter. The composition
root supplies the concrete provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import AuditEventStore, BreakGlassGrantRepository


@dataclass(frozen=True, slots=True)
class SecurityHttpRepositories:
    """Ports required by the security endpoints for one session."""

    audit: AuditEventStore
    grant: BreakGlassGrantRepository


class SecurityHttpDependencyProvider(Protocol):
    """Create a request-scoped security port bundle from a persistence session."""

    def for_session(self, session: object) -> SecurityHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "SecurityHttpDependencyProvider",
    "SecurityHttpRepositories",
]
