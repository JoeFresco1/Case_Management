"""SQL composition providers for the search HTTP boundaries.

These adapters are deliberately outside ``app.interfaces``. They bind
the local SQLAlchemy repositories, the portable LIKE search adapters,
and the field-key resolver query to the infra-free application
contracts; an Azure adapter can implement the same contracts later.

# INTEGRATION_SEAM: search engine

``SqlSearchProvider`` / ``SqlDocumentSearchProvider`` are the local
search-engine adapters. An Azure Cognitive Search / OpenSearch adapter
replaces the provider behind the same ``SearchHttpRepositories`` /
``DocumentSearchHttpRepositories`` bundle without touching the router.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.search.http_dependencies import (
    DocumentSearchHttpRepositories,
    SearchFieldKeyResolver,
    SearchHttpRepositories,
)
from app.domain.case_type import CaseTypeVersionStatus
from app.infrastructure.ai.noop_search_ranker import NoOpSearchRanker
from app.infrastructure.persistence.models import (
    CaseTypeVersion as CaseTypeVersionRow,
)
from app.infrastructure.persistence.repositories import (
    SqlCaseRepository,
    SqlCaseTypeVersionRepository,
    SqlSavedSearchAuditRepository,
    SqlSavedSearchRepository,
)
from app.infrastructure.persistence.search.sql_document_search_provider import (
    SqlDocumentSearchProvider,
)
from app.infrastructure.persistence.search.sql_search_provider import (
    SqlSearchProvider,
)


class SqlSearchFieldKeyResolver(SearchFieldKeyResolver):
    """Resolve searchable / sensitive field keys from published schemas.

    Reads every published ``CaseTypeVersion`` for the tenant and unions
    the field-key sets. This is the P05 contract (the schema is the
    source of truth for what is queryable / sensitive); the query is
    tenant-scoped (``WHERE tenant_id = :tenant_id``) like every other
    repository read.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Bind the resolver to the request's transaction session."""
        self._s = session

    async def field_keys(self, tenant_id: UUID) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return ``(searchable_keys, sensitive_keys)`` for ``tenant_id``."""
        stmt = select(CaseTypeVersionRow).where(
            CaseTypeVersionRow.tenant_id == tenant_id,
            CaseTypeVersionRow.status == CaseTypeVersionStatus.PUBLISHED.value,
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        searchable: set[str] = set()
        sensitive: set[str] = set()
        for row in rows:
            for field in row.schema_json.get("fields", []) if row.schema_json else []:
                if field.get("searchable"):
                    searchable.add(field["key"])
                if field.get("sensitivity") in {"restricted", "phi"}:
                    sensitive.add(field["key"])
        return tuple(sorted(searchable)), tuple(sorted(sensitive))


class SqlSearchHttpDependencyProvider:
    """Bind all case-search HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> SearchHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return SearchHttpRepositories(
            search=SqlSearchProvider(db),
            saved_search=SqlSavedSearchRepository(db),
            saved_search_audit=SqlSavedSearchAuditRepository(db),
            field_keys=SqlSearchFieldKeyResolver(db),
            # why: the deterministic core must run without an LLM
            # (FM-050 / AGENTS.md §4.R11). The no-op ranker preserves
            # the structured order with zero confidence; the Azure
            # ranker seam replaces this line when corp env is available.
            ranker=NoOpSearchRanker(),
        )


class SqlDocumentSearchHttpDependencyProvider:
    """Bind all in-case document-search HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> DocumentSearchHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return DocumentSearchHttpRepositories(
            document_search=SqlDocumentSearchProvider(db),
            case=SqlCaseRepository(db),
            case_type_version=SqlCaseTypeVersionRepository(db),
        )


__all__ = [
    "SqlDocumentSearchHttpDependencyProvider",
    "SqlSearchFieldKeyResolver",
    "SqlSearchHttpDependencyProvider",
]
