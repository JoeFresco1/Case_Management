"""Document search port (P03-US3 / P06-US8 / FM-033).

The in-case document search use case is the read-side companion to
``DocumentRepository.list_for_case``. It carries the same tenant /
authorization contract as the case search port: the adapter MUST
include ``WHERE tenant_id = :tenant_id`` on every query and MUST
honor the sensitivity threshold so a search never widens the
actor's visibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from app.domain.ports.core import TenantScoped


@dataclass(frozen=True, slots=True)
class DocumentSearchSpec:
    """A controlled, declarative in-case document search query
    (P03-US3 / P06-US8 / FM-033).

    The shape mirrors the case-search spec: free-text portion in
    ``text_query``; fielded filters on metadata columns
    (``classification``, ``uploaded_by``, ``uploaded_at`` range).

    ``allowed_classifications`` is the union of classification keys
    declared in the case-type's ``document_classifications``
    allowlist. The adapter projects ONLY documents whose
    ``classification`` is on the allowlist -- documents off-list are
    dropped silently (Constitution V; a leaked id cannot surface an
    unauthorized classification through the search surface).

    ``sensitivity_threshold`` is the lowest sensitivity value the
    result set is allowed to expose. When ``can_see_sensitive`` is
    ``False``, the adapter sets the threshold to ``RESTRICTED`` so
    any document at or above that level is omitted; restricted
    documents never appear in the result set.

    ``can_see_sensitive`` is the actor's pre-computed grant; the
    adapter projects redacted snippets (the file-name term hit is
    visible, but the surrounding content snippet is replaced with a
    ``Redacted`` marker) when ``False``.
    """

    tenant_id: UUID
    case_id: UUID
    text_query: str | None = None
    classification: str | None = None
    uploaded_by: UUID | None = None
    uploaded_since: datetime | None = None
    uploaded_until: datetime | None = None
    allowed_classifications: tuple[str, ...] = ()
    sensitivity_threshold: Any = "restricted"  # DocumentSensitivity on the adapter
    can_see_sensitive: bool = False
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class DocumentSearchHit:
    """A single document search hit (P03-US3 / FM-033).

    The hit carries the document identity, the case-attached
    metadata (file name, classification, sensitivity, current
    version, uploader, timestamps) and a bounded, redacted content
    snippet so the worker can see where the free-text term landed
    without exposing full content.
    """

    document_id: UUID
    case_id: UUID
    file_name: str
    classification: str
    sensitivity: str
    current_version: int
    content_type: str
    sha256: str
    uploaded_by: UUID
    uploaded_at: datetime
    # Snippet: a bounded excerpt of the matching content region. When
    # the document is restricted or above, the snippet is replaced
    # with the ``Redacted`` marker (P11-US6 / FM-029). The shape is
    # intentionally generic so the wire schema can render either an
    # excerpted string or a marker dict.
    snippet: Any


@dataclass(frozen=True, slots=True)
class DocumentSearchResultPage:
    """A page of document search hits (P03-US3 / FM-033)."""

    items: list[DocumentSearchHit]
    total_considered: int
    limit: int
    offset: int
    next_cursor: str | None


class DocumentSearchProvider(TenantScoped, Protocol):
    """The in-case document search engine abstraction (FM-033).

    The first slice uses a portable ``LIKE``-based adapter. Richer
    engines (PostgreSQL FTS, Azure Cognitive Search, OpenSearch)
    replace the implementation behind the same protocol -- the use
    case and router do not change.

    Contract:

    - Tenant scoped. Implementations MUST include
      ``WHERE tenant_id = :tenant_id`` on every query.
    - Portable. The LIKE adapter must not use ``ILIKE``,
      ``tsvector``, or any other dialect-specific feature
      (AGENTS.md §4.R1).
    - Classification-aware. Documents whose ``classification`` is not
      on ``spec.allowed_classifications`` MUST be silently dropped.
    - Sensitivity-aware. Documents strictly above
      ``spec.sensitivity_threshold`` MUST be silently dropped when
      ``spec.can_see_sensitive`` is ``False``. The wire snippet for
      a hit at the boundary carries a ``Redacted`` marker.
    """

    async def search_documents(self, spec: DocumentSearchSpec) -> DocumentSearchResultPage:
        """Execute an in-case document search and return a paginated page."""


__all__ = [
    "DocumentSearchHit",
    "DocumentSearchProvider",
    "DocumentSearchResultPage",
    "DocumentSearchSpec",
]
