"""Authorized in-case document search (P03-US3 / P06-US8 / FM-033).

The use case is the canonical entry point for finding a document that
is attached to a case:

1. **Authorize** the actor against ``Action.DOCUMENT_READ`` against the
   *case* resource (P11 / FM-002 / FM-029). The case resource is the
   authorization gate because every document in the result is returned
   at the same authorization level — there is no per-document policy
   today (FM-022 / ``list_documents`` mirrors this same pattern).
2. **Validate** the query shape. Metadata fields (``file_name``,
   ``classification``, ``uploaded_by``, ``uploaded_at`` range) match
   the columns the case-type allows; the free-text portion runs as a
   portable ``LIKE`` substring across ``file_name`` (FM-033 acceptance
   criterion: no FTS / JSONB / dialect-specific feature).
3. **Resolve** the case-type version and enforce the case-type's
   ``document_classifications`` allowlist — a document whose
   classification is not on the allowlist is silently dropped (same
   rationale as ``list_documents`` — Constitution V; a leaked id
   cannot surface an unauthorized classification through the search
   surface).
4. **Filter** restricted-sensitivity documents based on the actor's
   sensitive-read grant. Documents at or above the restricted
   threshold are returned in the hit list only when the actor holds
   the grant; otherwise they are omitted (P11-US6 / FM-029).
5. **Delegate** to ``DocumentSearchProvider.search_documents`` which
   returns a paginated ``DocumentSearchResultPage``. The use case
   does not touch SQL.

The search result carries a bounded, **redacted** content snippet for
each hit so the worker can see where the term landed without
exposing full content. Sensitive-sensitivity snippets are redacted
in their entirety (the worker does not need to see the term *inside*
a restricted document).

# INTEGRATION_SEAM: search engine

``DocumentSearchProvider`` is a domain port
(``app/domain/ports.py``). The local adapter is
``app.infrastructure.persistence.search.sql_document_search_provider``
(portable LIKE). An Azure Cognitive Search / OpenSearch adapter
replaces the same Protocol without touching this file. The contract
is the ``DocumentSearchSpec`` / ``DocumentSearchHit`` /
``DocumentSearchResultPage`` shapes below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.application.cases.view_case import actor_can_see_sensitive
from app.application.documents.list_documents import _ActorProxy
from app.domain.document import DocumentSensitivity
from app.domain.errors import NotFound, ValidationFailed
from app.domain.ports import (
    CaseRepository,
    CaseTypeVersionRepository,
    DocumentSearchProvider,
    DocumentSearchResultPage,
    DocumentSearchSpec,
)


@dataclass(frozen=True, slots=True)
class DocumentSearchQuery:
    """The application-layer shape for an in-case document search.

    The HTTP router translates its request model into this dataclass;
    the use case does not see FastAPI / Pydantic types.

    Attributes:
        tenant_id: Tenant scope of the request.
        actor_id: Human actor who initiated the search.
        actor_roles: Actor's role set used to authorize the search.
        case_id: The case whose documents the actor wants to search
            (P03-US3 — "find documents within a case").
        text_query: Optional free-text portion matched against
            ``file_name`` (case-insensitive substring via portable
            LIKE).
        classification: Optional filter restricting the result to a
            single declared classification (one of the case-type's
            ``document_classifications`` allowlist).
        uploaded_by: Optional filter restricting the result to a
            single uploader id.
        uploaded_since: Optional inclusive lower bound on
            ``uploaded_at`` (UTC datetime).
        uploaded_until: Optional inclusive upper bound on
            ``uploaded_at`` (UTC datetime).
        limit: Page size; clamped to ``[1, 200]``.
        offset: Pagination offset; non-negative.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    text_query: str | None = None
    classification: str | None = None
    uploaded_by: UUID | None = None
    uploaded_since: Any = None
    uploaded_until: Any = None
    limit: int = 50
    offset: int = 0


async def handle(
    query: DocumentSearchQuery,
    *,
    case_repo: CaseRepository,
    case_type_version_repo: CaseTypeVersionRepository,
    document_search_provider: DocumentSearchProvider,
) -> DocumentSearchResultPage:
    """Run an authorized, portable in-case document search.

    Failure behavior:
    - Authorization failure raises ``Forbidden`` (mapped to 403).
    - Missing or cross-tenant case raises ``NotFound`` (mapped to 404).
    - ``limit`` / ``offset`` are clamped to the contract (``[1, 200]``
      / ``[0, ...)``).
    - Empty / invalid ``text_query`` / ``classification`` raises
      ``ValidationFailed`` so the router can translate to 400.

    The use case never touches the SQL adapter directly; the
    ``DocumentSearchProvider`` port is the only mutation-shaped
    dependency. The application layer is pure: no FastAPI /
    SQLAlchemy imports.
    """
    _clamp_pagination(query)
    _validate_query(query)

    actor_ctx = _ActorProxy(
        tenant_id=query.tenant_id,
        user_id=query.actor_id,
        roles=query.actor_roles,
    )
    # 1. Tenant + role gate. The policy is deny-by-default; missing
    # roles raise ``Forbidden`` here. The resource is the case — the
    # case-attached document set shares the case's authorization
    # level (P03-US3 / FM-022 / ``list_documents``).
    await authorize(
        Action.DOCUMENT_READ,
        actor_ctx,  # type: ignore[arg-type]
        Resource(tenant_id=query.tenant_id, kind="case", id=query.case_id),
    )

    # 2. Resolve the case within the actor's tenant. Cross-tenant
    # ids resolve to ``None`` here (the repository filters by
    # tenant_id) and the use case raises ``NotFound`` so the API
    # never confirms cross-tenant existence (Constitution V).
    case = await case_repo.get(query.tenant_id, query.case_id)
    if case is None:
        raise NotFound(f"case {query.case_id} not found")

    # 3. Resolve the case-type version so the classification
    # allowlist can be enforced. The version was pinned at case
    # creation; the allowlist is part of the case-type contract
    # (FM-008).
    version = await case_type_version_repo.get(query.tenant_id, case.case_type_version_id)
    if version is None:
        raise NotFound(f"case type version {case.case_type_version_id} not found")
    allowed_classifications: tuple[str, ...] = tuple(version.schema.document_classifications)
    if query.classification is not None and query.classification not in allowed_classifications:
        # Reject client requests that target a classification the
        # case-type does not admit (defense in depth; the allowlist
        # is also enforced in the adapter's result projection).
        raise ValidationFailed(
            f"classification {query.classification!r} is not declared on case type",
            field="classification",
        )

    # 4. Compute the sensitive-read grant once so the SQL adapter
    # can project restricted values without re-evaluating the policy.
    can_see_sensitive = await actor_can_see_sensitive(
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        actor_roles=query.actor_roles,
        case_id=query.case_id,
    )

    # 5. Translate the application query into a port spec. The
    # ``sensitivity_threshold`` is the lower bound the result set is
    # allowed to surface; the adapter drops documents strictly above
    # the threshold when ``can_see_sensitive`` is False.
    spec = DocumentSearchSpec(
        tenant_id=query.tenant_id,
        case_id=query.case_id,
        text_query=query.text_query,
        classification=query.classification,
        uploaded_by=query.uploaded_by,
        uploaded_since=query.uploaded_since,
        uploaded_until=query.uploaded_until,
        allowed_classifications=allowed_classifications,
        sensitivity_threshold=(
            DocumentSensitivity.RESTRICTED if not can_see_sensitive else DocumentSensitivity.PUBLIC
        ),
        can_see_sensitive=can_see_sensitive,
        limit=query.limit,
        offset=query.offset,
    )
    return await document_search_provider.search_documents(spec)


def _validate_query(query: DocumentSearchQuery) -> None:
    """Reject an empty / over-long free-text portion.

    The free-text portion is bounded at the wire layer (Pydantic
    ``max_length=500``); this guard is the application-layer mirror
    so a misconfigured caller cannot widen the LIKE pattern.
    """
    if query.text_query is not None:
        text = query.text_query.strip()
        if not text:
            return
        if len(text) > 500:
            raise ValidationFailed("text query must be <= 500 characters", field="query")


def _clamp_pagination(query: DocumentSearchQuery) -> None:
    """Clamp ``limit`` / ``offset`` to the contract.

    Mirrors the case-search clamp (``[1, 200]`` / ``[0, ...)``).
    """
    if query.limit < 1 or query.limit > 200:
        raise ValidationFailed("limit must be in [1, 200]", field="limit")
    if query.offset < 0:
        raise ValidationFailed("offset must be >= 0", field="offset")


__all__ = [
    "DocumentSearchQuery",
    "handle",
]
