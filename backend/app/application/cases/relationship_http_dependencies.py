"""Dependency contracts for the case-relationship HTTP composition boundary.

The case-relationship router needs several ports bound to one request
transaction (case + case-type-version + document + relationship edge +
audit + outbox). This module describes that bundle without importing
FastAPI, SQLAlchemy, or any infrastructure adapter. The composition root
supplies the concrete provider.

Why a dedicated bundle (not ``CaseHttpRepositories``): the relationship
use cases need ``CaseRelationshipRepository`` (edge persistence), which
the case bundle does not carry. Extending the case bundle would force
every case endpoint to construct a repository it never uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.ports import (
    AuditEventStore,
    CaseRelationshipRepository,
    CaseRepository,
    CaseTypeRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
    OutboxWriter,
)


@dataclass(frozen=True, slots=True)
class CaseRelationshipHttpRepositories:
    """Ports required by the case-relationship HTTP endpoints for one session."""

    case: CaseRepository
    case_type: CaseTypeRepository
    case_type_version: CaseTypeVersionRepository
    document: DocumentRepository
    relationship: CaseRelationshipRepository
    audit: AuditEventStore
    outbox: OutboxWriter


class CaseRelationshipHttpDependencyProvider(Protocol):
    """Create a request-scoped case-relationship port bundle from a session."""

    def for_session(self, session: object) -> CaseRelationshipHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "CaseRelationshipHttpDependencyProvider",
    "CaseRelationshipHttpRepositories",
]
