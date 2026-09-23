"""Dependency contracts for the retention HTTP composition boundary.

The retention and retention-evidence routers need the session-bound
ports (case + audit) for one request transaction. The retention stores
themselves (policy, disposition, archival, freeze, orphan-proposal, and
the shared legal-hold store) are process-scoped — wired on ``app.state``
by the composition root — so they are resolved separately via the domain
ports in ``app.domain.ports``, never via an infrastructure import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import AuditEventStore, CaseRepository


@dataclass(frozen=True, slots=True)
class RetentionHttpRepositories:
    """Session-bound ports required by the retention HTTP endpoints."""

    case: CaseRepository
    audit: AuditEventStore


class RetentionHttpDependencyProvider(Protocol):
    """Create a request-scoped retention port bundle from a session."""

    def for_session(self, session: object) -> RetentionHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = ["RetentionHttpDependencyProvider", "RetentionHttpRepositories"]
