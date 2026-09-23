"""Dependency contracts for the admin reference-format HTTP boundary.

The router receives this small port bundle from the composition root. Keeping
the contract here prevents HTTP code from importing SQLAlchemy adapters while
preserving one transaction-bound audit store for each request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import AuditEventStore
from app.domain.reference_format import ReferenceFormatRepository


@dataclass(frozen=True, slots=True)
class AdminReferencesHttpRepositories:
    """Ports required by the admin reference-format endpoints for one session."""

    reference_format: ReferenceFormatRepository
    audit: AuditEventStore


class AdminReferencesHttpDependencyProvider(Protocol):
    """Create a request-scoped admin reference-format port bundle."""

    def for_session(self, session: object) -> AdminReferencesHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "AdminReferencesHttpDependencyProvider",
    "AdminReferencesHttpRepositories",
]
