"""Search & retrieval ports (P06 / FM-031).

The search engine is intentionally a separate port from
``CaseRepository``. The first-slice adapter uses portable ``LIKE`` (no
JSONB, no FTS, no pg-only operators -- AGENTS.md §4.R1 / plan §16.1).
Richer engines (PostgreSQL FTS, Azure Cognitive Search, OpenSearch) are
drop-in replacements behind the same protocol.

Tenant isolation: the contract is that EVERY search execution MUST be
scoped by ``tenant_id`` at the SQL level. Implementations MUST include
``WHERE tenant_id = :tenant_id`` on every query -- the same defense-in-
depth rule that applies to all repositories.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from app.domain.ports.core import TenantScoped


@dataclass(frozen=True, slots=True)
class SearchFilter:
    """A single fielded filter on the search query (P06-US1 / FM-031).

    Filters are applied as ``AND`` across the search query. The shape is
    the canonical, declarative query surface: ``field`` is the case's
    column (or ``"data.<key>"`` for case-type fields), ``op`` is one of
    ``eq``, ``like``, ``in`` and ``value`` is the operand.

    Why a controlled operator vocabulary: any expansion of the operator
    set must surface in the application layer (rejection of unknown
    operators) and in the SQL adapter (mapping to portable clauses). A
    caller asking for an unsupported operator gets a structured
    ``ValidationFailed`` rather than silently broadened semantics.
    """

    field: str
    op: str  # 'eq' | 'like' | 'in'
    value: Any


@dataclass(frozen=True, slots=True)
class SearchSpec:
    """A controlled, declarative case search query (P06-US1 / FM-031).

    The shape mirrors AGENTS.md §17.1 (declarative filter shape) and the
    pillar P06 spec. Free-text matches land in ``text_query``; fielded
    filters land in ``filters``; pagination lives on ``limit`` / ``offset``
    clamped by the application layer.

    The ``can_see_sensitive`` flag is the authorization decision
    computed *before* the search runs (the actor's roles determine
    whether sensitive fields are visible at all). The provider never
    re-evaluates authorization; the application hands the flag down
    so the SQL adapter can project redacted snippets without leaking
    field values through a side channel.

    ``sensitive_field_keys`` is the union of field keys declared at
    or above the restricted threshold across the actor's published
    CaseTypeVersions. The SQL adapter projects these values only when
    ``can_see_sensitive`` is ``True``; otherwise the value is replaced
    with the ``Redacted`` marker.

    ``searchable_field_keys`` is the union of field keys declared
    ``searchable`` in the actor's case type schema. The provider projects
    ONLY these fields into the ``data`` snippet; non-searchable fields
    are never returned. The caller (application) is responsible for
    resolving which keys are searchable given the actor's case types.
    """

    tenant_id: UUID
    text_query: str | None = None
    filters: tuple[SearchFilter, ...] = ()
    limit: int = 50
    offset: int = 0
    can_see_sensitive: bool = False
    searchable_field_keys: tuple[str, ...] = ()
    sensitive_field_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A single search hit (P06-US1 / FM-031).

    The hit carries the case identity, the canonical ``reference``, and
    a redacted ``data`` snippet. The snippet includes ONLY the fields
    declared ``searchable`` in the case type schema; non-searchable
    fields are omitted entirely so the result set cannot be used as a
    free-form exfiltration surface.
    """

    case_id: UUID
    reference: str
    priority: str
    status: str
    title: str
    # Redacted snippet: keys present only if the schema marks them
    # ``searchable``; restricted/PHI values are replaced with the
    # ``Redacted`` marker when ``can_see_sensitive`` was False on the
    # original query.
    data_snippet: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SearchResultPage:
    """A page of search hits (P06-US1 / FM-031)."""

    items: list[SearchHit]
    total_considered: int  # how many rows the engine scanned; advisory
    limit: int
    offset: int
    next_cursor: str | None


class SearchProvider(TenantScoped, Protocol):
    """The search engine abstraction (P06 / FM-031).

    The first slice uses a portable ``LIKE``-based adapter. Richer
    engines (PostgreSQL FTS, Azure Cognitive Search, OpenSearch) replace
    the implementation behind the same protocol -- the use case and
    router do not change.

    Contract:

    - Tenant scoped. Implementations MUST include
      ``WHERE tenant_id = :tenant_id`` on every query.
    - Portable. The LIKE adapter must not use ``ILIKE``, ``JSONB``
      operators, ``tsvector``, ``gen_random_uuid()``, or any other
      dialect-specific feature (AGENTS.md §4.R1).
    - Authorization-aware. ``spec.can_see_sensitive`` is the actor's
      pre-computed grant; the provider projects restricted values only
      when ``True``.
    - Searchable-aware. ``spec.searchable_field_keys`` is the actor's
      resolved set of declared-searchable field keys; non-searchable
      fields must never appear in the snippet.
    """

    async def search(self, spec: SearchSpec) -> SearchResultPage:
        """Execute a search and return a paginated result page."""


__all__ = [
    "SearchFilter",
    "SearchHit",
    "SearchProvider",
    "SearchResultPage",
    "SearchSpec",
]
