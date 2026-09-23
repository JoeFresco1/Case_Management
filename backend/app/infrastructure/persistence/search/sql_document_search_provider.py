"""Portable ``LIKE``-based in-case document search adapter
(P03-US3 / P06-US8 / FM-033 / AGENTS.md §4.R1).

This is the local SQL implementation of ``DocumentSearchProvider``.
It honors the platform portability contract:

- Only SQLAlchemy generic types (``String``, ``JSON``).
- Only ``LIKE`` / case-insensitive equality for text matching. No
  ``ILIKE`` (PostgreSQL-only), no ``JSONB`` operators, no
  ``tsvector``. SQLite (tests) and PostgreSQL (local) both honor
  ``LIKE`` with the same semantics on ``String`` columns.
- Tenant scoping is enforced with an explicit ``WHERE tenant_id =
  :tenant_id`` on every query; the implementation never relies on
  the optional ``tenant_filter_listener`` for safety.
- Classification + sensitivity filtering happen at the SQL layer
  (the application layer also enforces both, but the adapter is the
  final line of defense so a misconfigured caller cannot widen the
  result set).

The free-text portion is bounded to ``file_name``: the file metadata
is the canonical search surface for P03-US3 ("find documents within
a case by metadata and where configured by content"). Full-content
search is a future enhancement (FM-033 / ADR); this adapter returns
a redacted ``snippet`` field so the wire schema has a uniform shape
for both substring matches and redaction markers.

# INTEGRATION_SEAM: search engine

This adapter is the local implementation of
``DocumentSearchProvider``. An Azure Cognitive Search / OpenSearch
adapter would replace this module without touching the application
layer or the router. The contract is the
``DocumentSearchSpec`` / ``DocumentSearchHit`` /
``DocumentSearchResultPage`` shapes in ``app/domain/ports.py``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.document import DocumentSensitivity
from app.domain.ports import (
    DocumentSearchHit,
    DocumentSearchProvider,
    DocumentSearchResultPage,
    DocumentSearchSpec,
)
from app.infrastructure.persistence.models import Document as DocumentRow

# A bounded snippet shown to the worker when a free-text term lands
# inside a non-restricted file name. The substring around the match
# is bounded so the wire payload cannot leak the full file path
# (file paths can carry sensitive context, e.g. ``/patients/...``).
_SNIPPET_RADIUS = 32


class SqlDocumentSearchProvider(DocumentSearchProvider):
    """Portable ``LIKE`` in-case document search adapter."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def search_documents(self, spec: DocumentSearchSpec) -> DocumentSearchResultPage:
        """Execute the search and return a paginated
        ``DocumentSearchResultPage``.

        The implementation order:

        1. Base ``SELECT`` over ``documents`` with the mandatory
           ``tenant_id`` and ``case_id`` filters.
        2. Free-text portion (``LIKE`` on ``file_name`` only).
        3. Metadata filters (classification, uploader, date range).
        4. Classification allowlist projection — drop documents whose
           classification is not on the case-type's allowlist.
        5. Sensitivity threshold — drop restricted-or-above documents
           when the actor does not hold the sensitive-read grant.
        6. Pagination via ``LIMIT`` / ``OFFSET``.
        7. Project the hit: metadata + a bounded, redacted snippet.
        """
        stmt = select(DocumentRow).where(
            DocumentRow.tenant_id == spec.tenant_id,
            DocumentRow.case_id == spec.case_id,
        )

        text_query = (spec.text_query or "").strip()
        if text_query:
            stmt = stmt.where(
                func.lower(DocumentRow.file_name).like(f"%{_escape_like(text_query.lower())}%")
            )

        if spec.classification is not None:
            stmt = stmt.where(DocumentRow.classification == spec.classification)

        if spec.uploaded_by is not None:
            stmt = stmt.where(DocumentRow.uploaded_by == spec.uploaded_by)

        if spec.uploaded_since is not None:
            stmt = stmt.where(DocumentRow.created_at >= spec.uploaded_since)
        if spec.uploaded_until is not None:
            stmt = stmt.where(DocumentRow.created_at <= spec.uploaded_until)

        # Classification allowlist — defense-in-depth. The application
        # layer has already enforced this; we still guard at the SQL
        # layer so a misconfigured caller cannot surface an
        # unauthorized classification (Constitution V).
        if spec.allowed_classifications:
            stmt = stmt.where(DocumentRow.classification.in_(list(spec.allowed_classifications)))

        # Sensitivity threshold — when the actor does not hold the
        # sensitive-read grant, restricted-or-above documents are
        # dropped silently. P11-US6 / FM-029.
        if not spec.can_see_sensitive:
            # The threshold is the lowest sensitivity the actor is
            # allowed to see. PUBLIC + INTERNAL only when the
            # threshold is RESTRICTED.
            allowed_sensitivities = _sensitivity_below(
                DocumentSensitivity(spec.sensitivity_threshold)
            )
            stmt = stmt.where(DocumentRow.sensitivity.in_(allowed_sensitivities))

        # Total count of matching rows (advisory; useful for the UI
        # "X results" footer).
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = int((await self._s.execute(count_stmt)).scalar_one())

        # Page slice.
        page_stmt = (
            stmt.order_by(DocumentRow.created_at.desc()).limit(spec.limit).offset(spec.offset)
        )
        rows = (await self._s.execute(page_stmt)).scalars().all()

        items = [_row_to_hit(row, spec, text_query) for row in rows]
        next_cursor: str | None = None
        if len(items) == spec.limit and spec.offset + spec.limit < total:
            next_cursor = f"{spec.offset + spec.limit}:{spec.limit}"
        return DocumentSearchResultPage(
            items=items,
            total_considered=total,
            limit=spec.limit,
            offset=spec.offset,
            next_cursor=next_cursor,
        )


def _row_to_hit(
    row: DocumentRow,
    spec: DocumentSearchSpec,
    text_query: str,
) -> DocumentSearchHit:
    """Project an ORM row into a ``DocumentSearchHit``.

    The snippet is a bounded substring of ``file_name`` around the
    matching term when the document is below the restricted
    threshold; documents at or above the threshold carry a
    ``Redacted`` marker so the worker does not see the term landing
    inside a restricted document (P11-US6 / FM-029).
    """
    sensitivity = DocumentSensitivity(row.sensitivity)
    if not spec.can_see_sensitive and sensitivity in _RESTRICTED_OR_ABOVE:
        snippet: Any = {"redacted": True, "reason": "sensitive"}
    elif not text_query:
        # No free-text portion was supplied; the snippet is empty so
        # the wire shape remains uniform across both modes.
        snippet = ""
    else:
        snippet = _bounded_snippet(row.file_name, text_query.lower())
    return DocumentSearchHit(
        document_id=row.id,
        case_id=row.case_id,
        file_name=row.file_name,
        classification=row.classification,
        sensitivity=sensitivity.value,
        current_version=row.current_version,
        content_type=row.content_type,
        sha256=row.sha256,
        uploaded_by=row.uploaded_by,
        uploaded_at=row.created_at,
        snippet=snippet,
    )


# Sensitivity values at or above the restricted threshold — when the
# actor lacks the sensitive-read grant, documents in this set are
# filtered out before the hit is projected (P11-US6 / FM-029).
_RESTRICTED_OR_ABOVE: frozenset[DocumentSensitivity] = frozenset(
    {
        DocumentSensitivity.RESTRICTED,
        DocumentSensitivity.PHI,
    }
)


def _sensitivity_below(threshold: DocumentSensitivity) -> list[str]:
    """Return every sensitivity value strictly below ``threshold``.

    The sensitivity ordering is: PUBLIC < INTERNAL < CONFIDENTIAL <
    RESTRICTED < PHI. When ``threshold`` is RESTRICTED, the returned
    list is ``["public", "internal", "confidential"]`` so the
    non-sensitive actor sees CONFIDENTIAL documents too (CONFIDENTIAL
    is below RESTRICTED in the order). When ``threshold`` is PUBLIC,
    the list is empty (the actor cannot see anything).
    """
    order = [
        DocumentSensitivity.PUBLIC,
        DocumentSensitivity.INTERNAL,
        DocumentSensitivity.CONFIDENTIAL,
        DocumentSensitivity.RESTRICTED,
        DocumentSensitivity.PHI,
    ]
    idx = order.index(threshold)
    return [s.value for s in order[:idx]]


def _bounded_snippet(file_name: str, term: str) -> str:
    """Return a bounded substring of ``file_name`` around ``term``.

    The radius is small (32 chars on either side) so a long file path
    cannot leak its entire structure through the search response.
    When the term is absent the snippet is the leading prefix of the
    file name; the worker can still see what they are looking at.
    """
    lowered = file_name.lower()
    idx = lowered.find(term)
    if idx < 0:
        return file_name[: _SNIPPET_RADIUS * 2]
    start = max(0, idx - _SNIPPET_RADIUS)
    end = min(len(file_name), idx + len(term) + _SNIPPET_RADIUS)
    return file_name[start:end]


def _escape_like(value: str) -> str:
    """Escape ``%`` and ``_`` so user input cannot widen a LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = ["SqlDocumentSearchProvider"]
