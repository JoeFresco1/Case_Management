"""Pydantic schemas for the case search API (P06 / FM-031).

The wire shape is intentionally compact: a search hit exposes only
``id``, ``reference``, ``priority``, ``status``, ``title``, and a
bounded ``data`` snippet. The snippet keys are the declared-searchable
keys from the case-type schema; sensitive values are replaced by the
``RedactedField`` marker (P11-US6 / P05-US3 / FM-029).

Pagination follows the AGENTS.md §17.1 envelope: ``items``,
``next_cursor``, ``limit``, plus an advisory ``total_considered`` so
the UI can render an "X results" footer without a second round-trip.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.interfaces.http.schemas.cases import RedactedField


class SearchFilterRequest(BaseModel):
    """A single fielded filter (``field``, ``op``, ``value``).

    ``op`` is the controlled vocabulary defined by the application
    layer (``eq`` / ``like`` / ``in``). ``value`` is either a string
    or a list of strings; the router converts it to the right shape
    for the SQL adapter.
    """

    field: str = Field(min_length=1, max_length=128)
    op: str = Field(default="eq")
    value: Any


class CaseSearchRequest(BaseModel):
    """POST ``/api/v1/search`` body.

    ``query`` is the free-text portion (matched against ``title`` and
    the case's ``data`` snippet). ``filters`` are the controlled,
    declarative fielded filters. ``limit`` / ``offset`` clamp the
    page; the server clamps again to the contract.
    """

    query: str | None = Field(default=None, max_length=500)
    filters: list[SearchFilterRequest] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class CaseSearchHit(BaseModel):
    """A single search hit on the wire."""

    id: UUID
    reference: str
    priority: str
    status: str
    title: str
    # Snippet values may be the raw scalar OR a ``RedactedField``
    # marker. The router keys by the schema's declared sensitivity.
    data_snippet: dict[str, Any] = Field(default_factory=dict)


class CaseSearchResponse(BaseModel):
    """POST ``/api/v1/search`` and GET ``/api/v1/cases/search`` response.

    Follows the AGENTS.md §17.1 envelope plus an advisory
    ``total_considered`` for UI affordances. The cursor is opaque to
    the client; the next request echoes it back to advance the page.
    """

    items: list[CaseSearchHit]
    next_cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    total_considered: int = 0


# ---------------------------------------------------------------------------
# Saved searches + search audit (P06-US3, US5, US6 / FM-032)
# ---------------------------------------------------------------------------


class SortEntry(BaseModel):
    """A single ``(field, direction)`` sort entry on the wire."""

    field: str = Field(min_length=1, max_length=64)
    direction: str = Field(default="asc", pattern="^(asc|desc)$")


class SavedSearchRequest(BaseModel):
    """POST ``/api/v1/search/saved`` body.

    Mirrors the case-search request shape plus the human ``name`` the
    worker uses to recall the saved query. ``filters`` and ``sort``
    are optional; an empty saved search is a free-text bookmark.
    """

    name: str = Field(min_length=1, max_length=200)
    query: str | None = Field(default=None, max_length=500)
    filters: list[SearchFilterRequest] = Field(default_factory=list)
    sort: list[SortEntry] = Field(default_factory=list)


class SavedSearchResponse(BaseModel):
    """A persisted saved search on the wire.

    ``query_shape`` is the canonical ``{text_query, filters, sort}``
    dict the audit row references; the security report can correlate
    saves and runs without re-parsing the request.
    """

    id: UUID
    name: str
    query: str | None
    filters: list[SearchFilterRequest] = Field(default_factory=list)
    sort: list[SortEntry] = Field(default_factory=list)
    query_shape: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


class SavedSearchList(BaseModel):
    """A list of the actor's saved searches, newest first."""

    items: list[SavedSearchResponse]
    limit: int = Field(default=50, ge=1, le=200)


class SearchAuditEntryResponse(BaseModel):
    """A single search-audit row on the security-report wire shape.

    The query shape and the result count are returned verbatim so a
    security administrator can answer "who searched for what and saw
    how many results" (P06-US5 / FM-032).
    """

    id: UUID
    occurred_at: str
    actor_id: UUID
    saved_search_id: UUID | None
    query_shape: dict[str, Any] = Field(default_factory=dict)
    result_count: int = 0


class SearchAuditList(BaseModel):
    """A page of search-audit rows plus the standard envelope."""

    items: list[SearchAuditEntryResponse]
    limit: int = Field(default=50, ge=1, le=200)


# ---------------------------------------------------------------------------
# In-case document search (P03-US3 / P06-US8 / FM-033)
# ---------------------------------------------------------------------------


class DocumentSearchHit(BaseModel):
    """A single document search hit on the wire.

    The shape mirrors the ``DocumentSearchHit`` domain shape: the
    document identity + metadata, the bounded snippet (``Redacted``
    marker when the actor cannot see the content), and the audit
    timestamps. The worker can navigate to the document directly via
    the case workspace without re-issuing the search.
    """

    id: UUID
    case_id: UUID
    file_name: str
    classification: str
    sensitivity: str
    current_version: int
    content_type: str
    sha256: str
    uploaded_by: UUID
    uploaded_at: str
    # Snippet values may be the bounded excerpt OR a ``RedactedField``
    # marker. The router keys by the document's sensitivity.
    snippet: dict[str, Any] | str = ""


class DocumentSearchResponse(BaseModel):
    """GET ``/api/v1/cases/{id}/documents/search`` response.

    Follows the AGENTS.md §17.1 envelope plus an advisory
    ``total_considered`` so the UI can render an "X results" footer
    without a second round-trip.
    """

    items: list[DocumentSearchHit]
    next_cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    total_considered: int = 0


__all__ = [
    "CaseSearchHit",
    "CaseSearchRequest",
    "CaseSearchResponse",
    "DocumentSearchHit",
    "DocumentSearchResponse",
    "RedactedField",
    "SavedSearchList",
    "SavedSearchRequest",
    "SavedSearchResponse",
    "SearchAuditEntryResponse",
    "SearchAuditList",
    "SearchFilterRequest",
    "SortEntry",
]
