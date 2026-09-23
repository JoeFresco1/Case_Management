"""Dependency contracts for the admin role HTTP composition boundary.

The role router needs role persistence and audit ports bound to one request
transaction. This module describes that bundle without importing FastAPI,
SQLAlchemy, or any infrastructure adapter. The composition root supplies the
concrete provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import AuditEventStore, RoleRepository


@dataclass(frozen=True, slots=True)
class AdminRolesHttpRepositories:
    """Ports required by the admin role endpoints for one session."""

    role: RoleRepository
    audit: AuditEventStore


class AdminRolesHttpDependencyProvider(Protocol):
    """Create a request-scoped admin role port bundle from a session."""

    def for_session(self, session: object) -> AdminRolesHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "AdminRolesHttpDependencyProvider",
    "AdminRolesHttpRepositories",
]
