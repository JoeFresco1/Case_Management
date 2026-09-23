"""Canonical search domain vocabulary (P06 / FM-031, FM-032).

This module is the single domain entry point for search: it owns the
pure query/result/audit shapes plus the two result-time rules that keep
search from becoming an access-control bypass (Constitution V):

- **Authorization at result time**: :func:`authorize_results` drops
  cross-tenant rows and rows the actor may not see, even when the
  underlying engine returned them.
- **Snippet redaction by reference id**: :func:`redact_snippet` masks
  restricted values with ``{"redacted": True, "ref": <key>}`` markers,
  never leaking content (FM-011).

# Why this module exists alongside the ports

- :mod:`app.domain.ports.search` owns the ``SearchProvider`` engine
  abstraction (``SearchSpec`` / ``SearchHit`` / ``SearchResultPage``).
  It is re-exported here so callers have one import surface.
- :mod:`app.domain.saved_search` owns the ``SavedSearch`` aggregate
  (``SavedSearch`` / ``SortSpec``). It is re-exported here for the same
  reason; this module does not duplicate its invariants.
- This module adds what neither port owns: the actor-bound
  :class:`SearchQuery`, the tenant-carrying :class:`SearchResult`, the
  :class:`SearchAuditEvent`, and the pure result-time policy functions
  the application layer calls after the engine returns.

# LIKE-search provider plan (portable, AGENTS.md R1)

The first engine behind ``SearchProvider`` is the portable ``LIKE``
adapter (``app.infrastructure.persistence.search.sql_search_provider``).
The plan, expressed as domain constraints the adapter must honor:

1. **Tenant scoping**: every SQL statement carries
   ``WHERE tenant_id = :tenant_id``. The domain rule
   (:func:`authorize_results`) re-checks tenant at result time, so a
   missing ``WHERE`` degrades to empty results, never to a leak.
2. **Free text**: ``LIKE`` on ``cases.title`` only
   (``LOWER(title) LIKE LOWER(:pattern)``). ``data`` free-text is
   rejected: JSON-TEXT matching is dialect-brittle and bleeds false
   positives across unrelated keys.
3. **Fielded filters**: controlled vocabulary ``eq`` / ``like`` / ``in``.
   Core columns map to SQL columns; ``data.<key>`` filters match the
   JSON-TEXT substring ``"key":"value"`` via portable ``LIKE``. No
   ``ILIKE``, ``JSONB`` operators, ``tsvector``, or
   ``gen_random_uuid()`` (plan.md:289).
4. **Searchable gating**: only keys declared ``searchable`` in the
   published ``CaseTypeVersion`` schema may appear in a filter or a
   snippet. Non-searchable fields are rejected at query validation
   (:class:`SearchQuery`) and omitted from snippets
   (:func:`redact_snippet` keeps only declared keys upstream).
5. **Redaction**: the adapter projects snippets for searchable keys
   only; restricted values become ``ref`` markers unless
   ``can_see_sensitive`` is ``True``. The pure :func:`redact_snippet`
   is the testable kernel of that projection.
6. **Pagination**: ``limit`` in ``[1, 200]`` (default 50), ``offset``
   ``>= 0``; cursor is ``"<offset>:<limit>"`` when a full page ships.
7. **Audit**: every execution appends a :class:`SearchAuditEvent`
   carrying the canonical query shape (sensitive values replaced by
   ``ref`` markers) plus the result count -- never payload PHI.
8. **Future engines** (Postgres FTS, Azure Cognitive Search,
   OpenSearch) replace the adapter behind the same ``SearchProvider``
   Protocol. The domain shapes and result-time rules do not change.

# INTEGRATION_SEAM: search engine

``SearchProvider`` is the seam. Local: ``SqlSearchProvider``
(portable LIKE). Production: ``AzureCognitiveSearchProvider`` (not yet
implemented; same Protocol, ``WHERE tenant_id`` preserved server-side).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

from app.domain.errors import ValidationFailed
from app.domain.ports.search import (
    SearchFilter,
    SearchHit,
    SearchProvider,
    SearchResultPage,
    SearchSpec,
)
from app.domain.saved_search import SavedSearch, SortSpec
from app.domain.sensitivity import SensitiveReference

# Controlled operator vocabulary. Any expansion must land in the SQL
# adapter dispatch AND the OpenAPI schema at the same time; an unknown
# operator is a client bug surfaced as ``ValidationFailed``.
SUPPORTED_OPS: Final[frozenset[str]] = frozenset({"eq", "like", "in"})

# Core case columns filterable without consulting the case-type schema.
# Each maps to a SQL column on the ``cases`` table in the adapter.
CORE_FIELDS: Final[frozenset[str]] = frozenset(
    {"reference", "status", "priority", "title", "current_workflow_state"}
)

# Pagination contract (AGENTS.md 17.1 / P06 Q1).
DEFAULT_LIMIT: Final[int] = 50
MAX_LIMIT: Final[int] = 200


@dataclass(frozen=True, slots=True)
class QueryFilter:
    """One fielded filter inside a :class:`SearchQuery`.

    ``field`` is a core column (``status``, ``priority``, ...) or
    ``"data.<key>"`` for a case-type field. ``op`` is one of
    :data:`SUPPORTED_OPS`. ``value`` is the operand (scalar for
    ``eq``/``like``, non-empty list for ``in``).
    """

    field: str
    op: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        """Render the wire/canonical dict for audit replay."""
        return {"field": self.field, "op": self.op, "value": self.value}


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Actor-bound declarative search request (P06-US1/US2 / FM-031).

    Attributes:
        tenant_id: Tenant the search executes in. Threaded into the
            engine spec and re-checked at result time.
        actor_id: Actor performing the search. Recorded on the audit
            event; authorization itself lives in the application layer
            (``Action.CASE_READ`` deny-by-default) plus
            :func:`authorize_results` at result time.
        text_query: Free-text portion (matched on title by the LIKE
            adapter); ``None`` means fielded-only.
        filters: Tuple of :class:`QueryFilter`; AND-ed by the engine.
        sort: Tuple of :class:`SortSpec`; empty means engine default
            (newest first).
        limit: Page size, clamped to ``[1, 200]``.
        offset: Page offset, ``>= 0``.
        searchable_field_keys: Union of ``searchable=True`` keys across
            the tenant's published versions (resolved by the caller from
            the schema store, P05/P06).
        sensitive_field_keys: Union of restricted/phi keys across the
            tenant's published versions (resolved by the caller).

    Failure mode: construction raises :class:`ValidationFailed` for an
    out-of-range page, an unknown operator, an empty ``in`` list, or a
    filter on a non-searchable field (fail-closed, never silently
    broadened).
    """

    tenant_id: UUID
    actor_id: UUID
    text_query: str | None = None
    filters: tuple[QueryFilter, ...] = ()
    sort: tuple[SortSpec, ...] = ()
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    searchable_field_keys: tuple[str, ...] = ()
    sensitive_field_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate pagination, operators, and searchable gating."""
        # why: the SQL adapter never reasons about out-of-range pages;
        # rejecting here keeps the engine total-order deterministic.
        if self.limit < 1 or self.limit > MAX_LIMIT:
            raise ValidationFailed("limit must be in [1, 200]", field="limit")
        if self.offset < 0:
            raise ValidationFailed("offset must be >= 0", field="offset")
        searchable = set(self.searchable_field_keys) | set(CORE_FIELDS)
        for filt in self.filters:
            # why: narrow vocabulary first so a typo'd operator cannot
            # fall through to a silently broader match.
            if filt.op not in SUPPORTED_OPS:
                raise ValidationFailed(
                    f"unsupported filter operator {filt.op!r} for field {filt.field!r}; "
                    f"supported: {sorted(SUPPORTED_OPS)}",
                    field="filters",
                )
            if not filt.field or not isinstance(filt.field, str):
                raise ValidationFailed("filter field must be a non-empty string", field="filters")
            if filt.op == "in":
                seq = filt.value if isinstance(filt.value, (list, tuple)) else None
                # why: an empty ``in`` list matches nothing on some
                # engines and everything on others; rejecting removes
                # the dialect-dependent edge.
                if seq is None or len(seq) == 0:
                    raise ValidationFailed(
                        f"operator 'in' on field {filt.field!r} requires a non-empty list",
                        field="filters",
                    )
            # why: searchable gating at construction keeps a saved row
            # executable -- a non-searchable filter can never reach the
            # engine or leak through a snippet side channel.
            key = filt.field[len("data.") :] if filt.field.startswith("data.") else filt.field
            if key not in searchable:
                raise ValidationFailed(
                    f"field {filt.field!r} is not declared searchable",
                    field="filters",
                )


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One tenant-carrying search hit (P06-US1 / FM-031).

    ``tenant_id`` rides on the hit (not just the query) so the pure
    :func:`authorize_results` can drop cross-tenant rows without I/O.
    ``snippet`` carries ONLY declared-searchable keys; restricted values
    are ``ref`` markers unless the actor holds the sensitive-read grant
    (see :func:`redact_snippet`).
    """

    tenant_id: UUID
    case_id: UUID
    reference: str
    status: str
    priority: str
    title: str
    snippet: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchAuditEvent:
    """Audit fact for one search execution (P06-US5/US6 / FM-032).

    Carries the canonical query shape (sensitive values replaced by
    ``ref`` markers, never content), the result count, and the actors.
    ``saved_search_id`` is ``None`` for ad-hoc searches.

    Attributes:
        id: Stable event id, assigned by the factory.
        tenant_id: Tenant the search executed in.
        actor_id: Actor who searched.
        saved_search_id: Saved-search row re-executed, if any.
        query_shape: Canonical shape (text + filters + sort); safe to
            persist and show to security staff.
        result_count: Number of hits returned after result-time policy.
        occurred_at: UTC timestamp of the execution.
    """

    id: UUID
    tenant_id: UUID
    actor_id: UUID
    saved_search_id: UUID | None
    query_shape: dict[str, Any]
    result_count: int
    occurred_at: datetime


def query_shape_for_audit(query: SearchQuery) -> dict[str, Any]:
    """Build the canonical, PHI-free query shape for audit rows.

    Args:
        query: The validated search request.

    Returns:
        Dict with ``text_query``, ``filters`` (each ``field``/``op``
        plus value, or a ``ref`` marker when the filter targets a
        sensitive key), and ``sort``. Security staff can replay the
        shape without ever seeing protected content.

    Failure mode: pure projection; never raises on unknown keys.
    """
    sensitive = set(query.sensitive_field_keys)
    shaped_filters: list[dict[str, Any]] = []
    for filt in query.filters:
        key = filt.field[len("data.") :] if filt.field.startswith("data.") else filt.field
        # why: filter values can themselves be PHI (e.g. searching an
        # ssn value); the audit row records *which* sensitive key was
        # queried (the ref) without recording the value.
        if key in sensitive:
            shaped_filters.append(
                {
                    "field": filt.field,
                    "op": filt.op,
                    "value": SensitiveReference(field_key=key).to_marker(),
                }
            )
        else:
            shaped_filters.append(filt.to_dict())
    return {
        "text_query": query.text_query,
        "filters": shaped_filters,
        "sort": [s.to_dict() for s in query.sort],
    }


def build_audit_event(
    query: SearchQuery,
    *,
    result_count: int,
    occurred_at: datetime,
    saved_search_id: UUID | None = None,
) -> SearchAuditEvent:
    """Build the audit fact for one search execution.

    Args:
        query: The validated search request.
        result_count: Hits returned after result-time policy.
        occurred_at: UTC timestamp of the execution.
        saved_search_id: Saved-search row re-executed, if any.

    Returns:
        A frozen :class:`SearchAuditEvent` with a fresh id and the
        PHI-free shape from :func:`query_shape_for_audit`.

    Failure mode: pure construction; never raises.
    """
    return SearchAuditEvent(
        id=uuid4(),
        tenant_id=query.tenant_id,
        actor_id=query.actor_id,
        saved_search_id=saved_search_id,
        query_shape=query_shape_for_audit(query),
        result_count=result_count,
        occurred_at=occurred_at,
    )


def authorize_results(
    results: tuple[SearchResult, ...] | list[SearchResult],
    *,
    tenant_id: UUID,
    visible_case_ids: frozenset[UUID] | set[UUID],
) -> tuple[SearchResult, ...]:
    """Enforce authorization at result time (Constitution V).

    Drops every hit whose ``tenant_id`` differs from the requesting
    tenant AND every hit whose ``case_id`` is absent from
    ``visible_case_ids`` (the application-computed set of cases the
    actor may read). The engine is never trusted to have filtered
    correctly; this is the second, determinative gate.

    Args:
        results: Candidate hits from the engine.
        tenant_id: Requesting tenant.
        visible_case_ids: Case ids the actor may read (computed by the
            application from ``Action.CASE_READ`` grants).

    Returns:
        The authorized subset, preserving engine order.

    Failure mode: pure filter; never raises. Empty grants yield empty
        results (deny-by-default).
    """
    allowed: list[SearchResult] = []
    for hit in results:
        # why tenant first: cross-tenant is denied unconditionally even
        # when the id set would otherwise contain the row
        # (defense-in-depth over ``WHERE tenant_id``).
        if hit.tenant_id != tenant_id:
            continue
        if hit.case_id not in visible_case_ids:
            continue
        allowed.append(hit)
    return tuple(allowed)


def redact_snippet(
    snippet: dict[str, Any],
    *,
    sensitive_keys: frozenset[str] | set[str] | tuple[str, ...],
    can_see_sensitive: bool,
) -> dict[str, Any]:
    """Redact a result snippet's sensitive excerpts by reference id.

    Args:
        snippet: Field key -> excerpt text (searchable keys only).
        sensitive_keys: Keys declared restricted/phi for the tenant.
        can_see_sensitive: Actor's pre-computed sensitive-read grant.

    Returns:
        A fresh dict; restricted excerpts become ``ref`` markers naming
        the field, never the content. The input is never mutated so the
        redacted view cannot be written back.

    Failure mode: pure projection; never raises.
    """
    if can_see_sensitive:
        return dict(snippet)
    sensitive = set(sensitive_keys)
    if not sensitive:
        return dict(snippet)
    out: dict[str, Any] = {}
    for key, value in snippet.items():
        # why key-based, never content-based: an innocuous value under
        # a restricted key is still masked, while a public key whose
        # text looks sensitive passes through untouched.
        if key in sensitive:
            out[key] = SensitiveReference(field_key=key).to_marker()
        else:
            out[key] = value
    return out


def apply_result_time_policy(
    results: tuple[SearchResult, ...] | list[SearchResult],
    *,
    tenant_id: UUID,
    visible_case_ids: frozenset[UUID] | set[UUID],
    sensitive_keys: frozenset[str] | set[str] | tuple[str, ...],
    can_see_sensitive: bool,
) -> tuple[SearchResult, ...]:
    """Compose result-time authorization and snippet redaction.

    Applies :func:`authorize_results` first (invisible rows vanish
    entirely, snippets included), then :func:`redact_snippet` per
    surviving hit. This is the single call the application layer makes
    after the engine returns, so the two rules cannot drift apart.

    Args:
        results: Candidate hits from the engine.
        tenant_id: Requesting tenant.
        visible_case_ids: Case ids the actor may read.
        sensitive_keys: Restricted/phi keys for the tenant.
        can_see_sensitive: Actor's sensitive-read grant.

    Returns:
        Authorized, redacted hits in engine order. Fresh
        :class:`SearchResult` rows; inputs are never mutated.

    Failure mode: pure composition; never raises.
    """
    authorized = authorize_results(results, tenant_id=tenant_id, visible_case_ids=visible_case_ids)
    redacted: list[SearchResult] = []
    for hit in authorized:
        redacted.append(
            SearchResult(
                tenant_id=hit.tenant_id,
                case_id=hit.case_id,
                reference=hit.reference,
                status=hit.status,
                priority=hit.priority,
                title=hit.title,
                snippet=redact_snippet(
                    hit.snippet,
                    sensitive_keys=sensitive_keys,
                    can_see_sensitive=can_see_sensitive,
                ),
            )
        )
    return tuple(redacted)


def to_provider_spec(query: SearchQuery, *, can_see_sensitive: bool) -> SearchSpec:
    """Translate a domain query into the engine ``SearchSpec``.

    Args:
        query: The validated search request.
        can_see_sensitive: Actor's pre-computed sensitive-read grant
            (computed by the application; the provider never
            re-evaluates authorization).

    Returns:
        A ``SearchSpec`` the ``SearchProvider`` can execute.

    Failure mode: pure translation; never raises.
    """
    return SearchSpec(
        tenant_id=query.tenant_id,
        text_query=query.text_query,
        filters=tuple(
            SearchFilter(field=filt.field, op=filt.op, value=filt.value) for filt in query.filters
        ),
        limit=query.limit,
        offset=query.offset,
        can_see_sensitive=can_see_sensitive,
        searchable_field_keys=query.searchable_field_keys,
        sensitive_field_keys=query.sensitive_field_keys,
    )


def from_provider_hit(hit: SearchHit, *, tenant_id: UUID) -> SearchResult:
    """Lift an engine hit into the tenant-carrying domain result.

    Args:
        hit: Engine hit (no tenant; the engine is tenant-scoped by
            construction).
        tenant_id: Requesting tenant, stamped onto the result so
            :func:`authorize_results` can verify it.

    Returns:
        A :class:`SearchResult` with the engine snippet verbatim;
        redaction happens in :func:`apply_result_time_policy`.

    Failure mode: pure lift; never raises.
    """
    return SearchResult(
        tenant_id=tenant_id,
        case_id=hit.case_id,
        reference=hit.reference,
        status=hit.status,
        priority=hit.priority,
        title=hit.title,
        snippet=dict(hit.data_snippet),
    )


# Re-exported port types so ``app.domain.search`` is the single import
# surface for the search domain. The Protocol itself lives in
# ``app.domain.ports.search`` (the engine seam); the ``SavedSearch``
# aggregate lives in ``app.domain.saved_search``. The imports above
# bind those names here; listing them in ``__all__`` below makes the
# re-export explicit for both readers and type-checkers.

__all__ = [
    "CORE_FIELDS",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "SUPPORTED_OPS",
    "QueryFilter",
    "SavedSearch",
    "SearchAuditEvent",
    "SearchFilter",
    "SearchHit",
    "SearchProvider",
    "SearchQuery",
    "SearchResult",
    "SearchResultPage",
    "SearchSpec",
    "SortSpec",
    "apply_result_time_policy",
    "authorize_results",
    "build_audit_event",
    "from_provider_hit",
    "query_shape_for_audit",
    "redact_snippet",
    "to_provider_spec",
]
