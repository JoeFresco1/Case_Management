"""Dependency contracts for the legal-hold HTTP composition boundary.

The holds router needs the session-bound ports (case + audit) for one
request transaction. The legal-hold repository itself is process-scoped
(a single shared store across requests, wired on ``app.state`` by the
composition root) so it is resolved separately via the ``LegalHoldRepository``
domain port — never via an infrastructure import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import AuditEventStore, CaseRepository


@dataclass(frozen=True, slots=True)
class HoldHttpRepositories:
    """Session-bound ports required by the legal-hold HTTP endpoints."""

    case: CaseRepository
    audit: AuditEventStore


class HoldHttpDependencyProvider(Protocol):
    """Create a request-scoped legal-hold port bundle from a session."""

    def for_session(self, session: object) -> HoldHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = ["HoldHttpDependencyProvider", "HoldHttpRepositories"]
