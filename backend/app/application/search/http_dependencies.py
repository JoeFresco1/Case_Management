"""Dependency contracts for the search HTTP composition boundary.

The case-search router (``app.interfaces.http.routers.search``) and the
in-case document-search router (``app.interfaces.http.routers.document_search``)
need several ports bound to one request transaction. This module describes
those bundles without importing FastAPI, SQLAlchemy, or any infrastructure
adapter. The composition root supplies the concrete providers.

# Why a field-key resolver port

The case-search use case consumes the tenant's ``(searchable_keys,
sensitive_keys)`` sets as sealed inputs resolved from the published
``CaseTypeVersion`` schemas (the P05 contract). The domain
``CaseTypeVersionRepository`` port has no tenant-wide
``list-published`` read, and the application layer is pure (no SQL).
The ``SearchFieldKeyResolver`` port below is the narrow seam that
carries that single query so the router stays free of
``app.infrastructure`` imports; the SQL implementation lives in
``app.infrastructure.persistence.search_http_dependencies`` and an
Azure implementation can replace it without touching the router.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.application.ai.search_ranking import SearchRanker
from app.domain.ports import (
    CaseRepository,
    CaseTypeVersionRepository,
    DocumentSearchProvider,
    SavedSearchAuditRepository,
    SavedSearchRepository,
    SearchProvider,
)


class SearchFieldKeyResolver(Protocol):
    """Resolve the tenant's searchable / sensitive field-key sets."""

    async def field_keys(self, tenant_id: UUID) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return ``(searchable_keys, sensitive_keys)`` for ``tenant_id``.

        The sets are the union of ``searchable=true`` keys and of keys
        declared at or above the restricted sensitivity threshold
        across the tenant's published ``CaseTypeVersion`` schemas.
        Both tuples are sorted so the downstream use case sees a
        deterministic input.
        """

        ...


@dataclass(frozen=True, slots=True)
class SearchHttpRepositories:
    """Ports required by the case-search HTTP endpoints for one session."""

    search: SearchProvider
    saved_search: SavedSearchRepository
    saved_search_audit: SavedSearchAuditRepository
    field_keys: SearchFieldKeyResolver
    ranker: SearchRanker


class SearchHttpDependencyProvider(Protocol):
    """Create a request-scoped case-search port bundle from a session."""

    def for_session(self, session: object) -> SearchHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


@dataclass(frozen=True, slots=True)
class DocumentSearchHttpRepositories:
    """Ports required by the in-case document-search endpoint for one session."""

    document_search: DocumentSearchProvider
    case: CaseRepository
    case_type_version: CaseTypeVersionRepository


class DocumentSearchHttpDependencyProvider(Protocol):
    """Create a request-scoped document-search port bundle from a session."""

    def for_session(self, session: object) -> DocumentSearchHttpRepositories:
        """Return adapters sharing ``session`` for one HTTP request."""

        ...


__all__ = [
    "DocumentSearchHttpDependencyProvider",
    "DocumentSearchHttpRepositories",
    "SearchFieldKeyResolver",
    "SearchHttpDependencyProvider",
    "SearchHttpRepositories",
]
