"""Saved-search use cases (P06-US3, US5, US6 / FM-032).

The application layer is the only place that orchestrates the
``SavedSearch`` aggregate, the ``SavedSearchAudit`` row, and the
re-execution of the saved query against the same ``SearchProvider``
port the live case search consumes (FM-031).

# Use cases

- ``save_search`` — the worker submits a query shape (text + filters +
  sort) under a name. The use case persists the row and emits the
  audit row that marks the save action.
- ``list_saved_searches`` — read the owner's saved searches. No audit
  row: the listing is a UI affordance, not a search activity.
- ``delete_saved_search`` — owner-only delete; the row is removed and
  no audit row is emitted (the save itself is what was audited).
- ``run_saved_search`` — the worker re-executes a previously saved
  query. The use case authorizes the actor against ``Action.CASE_READ``
  (same path the live search takes), re-runs the query through the
  ``SearchProvider``, and appends a ``SavedSearchAudit`` row carrying
  the actor, the saved-search id, the canonical query shape, the
  timestamp, and the result count (P06-US6 / FM-032).

# Audit surface

The security-staff read path (``view_search_audit``) is a thin
projection over ``SavedSearchAuditRepository.list_for_security_report``.
The actor must hold ``Action.AUDIT_READ``; the use case re-evaluates
the policy so a misconfigured router cannot leak audit rows.

# Why no application-layer integration test seam

The ``case_search.handle`` callable is reused through a thin
function-pointer seam (``_execute``) so the in-memory test fake can
skip the live case-search use case and exercise the saved-search
behavior without a SQL adapter. The seam is intentionally narrow:
just the callable the application layer would otherwise call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.application.search.case_search import CaseSearchQuery
from app.application.search.case_search import handle as execute_case_search
from app.domain.context import Clock, UserContext
from app.domain.errors import NotFound, ValidationFailed
from app.domain.ports import (
    SavedSearchAuditRepository,
    SavedSearchRepository,
    SearchFilter,
    SearchProvider,
    SearchResultPage,
    SearchSpec,
)
from app.domain.saved_search import SORTABLE_FIELDS, SavedSearch, SortSpec

# Default page size for the audit report (AGENTS.md §17.1).
DEFAULT_AUDIT_PAGE_LIMIT: int = 50
MAX_AUDIT_PAGE_LIMIT: int = 200

# Default page size for the saved-search listing (AGENTS.md §17.1).
DEFAULT_LIST_PAGE_LIMIT: int = 50
MAX_LIST_PAGE_LIMIT: int = 200

# The application layer's hook into the live search execution. The
# tests substitute this with an in-memory implementation; the
# production code path goes through ``execute_case_search``.
ExecuteSearch = Callable[[CaseSearchQuery], Awaitable[SearchResultPage]]


def _default_execute(
    query: CaseSearchQuery,
    *,
    search_provider: SearchProvider,
) -> Awaitable[SearchResultPage]:
    """Build an async callable around :func:`execute_case_search`.

    Returning an awaitable keeps the seam uniform: tests substitute an
    awaitable-returning function; the production path uses the same
    shape so no call-site branching is needed.
    """
    return execute_case_search(query, search_provider=search_provider)


# ---------------------------------------------------------------------------
# save_search
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SaveSearchCommand:
    """The input to ``save_search.handle``.

    Mirrors the wire shape: a name plus the query shape (text + filters
    + sort). The use case validates the shape against the application
    search's controlled operator vocabulary so a saved row is always
    executable (the SQL adapter would refuse an unknown op at run time,
    but we want the rejection at save time too).
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    name: str
    text_query: str | None
    filters: tuple[tuple[str, str, object], ...]
    sort: tuple[SortSpec, ...] = ()


def _validate_command(cmd: SaveSearchCommand) -> None:
    """Run application-level invariants on ``cmd``.

    Mirrors ``case_search.handle``'s operator/filter validation so a
    saved search cannot smuggle an unsupported operator or an empty
    ``in`` operand; the SQL adapter would refuse either at run time but
    we want the failure at save time so the worker gets a clean 400
    instead of a delayed 422 on reuse.
    """
    if not cmd.name or not cmd.name.strip():
        raise ValidationFailed("name is required", field="name")
    if len(cmd.name) > 200:
        raise ValidationFailed("name must be at most 200 characters", field="name")
    for f in cmd.filters:
        if not f[0] or not isinstance(f[0], str):
            raise ValidationFailed("filter field must be a non-empty string", field="filters")
        if f[1] not in {"eq", "like", "in"}:
            raise ValidationFailed(
                f"unsupported operator {f[1]!r}; supported: eq, like, in",
                field="filters",
            )
        if f[1] == "in" and (not isinstance(f[2], (list, tuple)) or len(f[2]) == 0):
            raise ValidationFailed(
                f"operator 'in' on field {f[0]!r} requires a non-empty list",
                field="filters",
            )
    for s in cmd.sort:
        if s.field not in SORTABLE_FIELDS:
            raise ValidationFailed(
                f"unsupported sort field {s.field!r}",
                field="sort",
            )


async def save_search(
    cmd: SaveSearchCommand,
    *,
    repository: SavedSearchRepository,
    audit: SavedSearchAuditRepository,
    clock: Clock,
) -> SavedSearch:
    """Persist a new saved-search row and audit the save.

    The same audit row schema is reused for the save action with
    ``result_count=0`` and ``saved_search_id`` set to the freshly
    minted id, so a security administrator's report shows "the user
    saved a query shape X at time Y" alongside the executions.

    The function does NOT commit; the caller commits once at the
    boundary so the saved-search row, the audit row, and any outbox
    row share a single transaction (AGENTS.md §4.R12, plan §4.R12).
    """
    _validate_command(cmd)
    actor = UserContext(
        user_id=cmd.actor_id,
        tenant_id=cmd.tenant_id,
        display_name="",
        roles=cmd.actor_roles,
    )
    # Authorize against CASE_READ: a worker who cannot read cases
    # cannot save one either. The cross-tenant check is the policy's
    # first gate so a misconfigured caller cannot smuggle a row into
    # another tenant.
    await authorize(
        Action.CASE_READ,
        actor,
        Resource(tenant_id=cmd.tenant_id, kind="saved_search"),
    )
    now = clock.now()
    saved = SavedSearch.create(
        tenant_id=cmd.tenant_id,
        owner_user_id=cmd.actor_id,
        name=cmd.name,
        text_query=cmd.text_query,
        filters=cmd.filters,
        sort=cmd.sort,
        now=now,
    )
    await repository.add(saved)
    # Audit the save action: ``saved_search_id`` is the freshly minted
    # id so the security report can correlate save and run events.
    await audit.append(
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        saved_search_id=saved.id,
        query_shape=saved.query_shape,
        result_count=0,
        occurred_at=now,
    )
    return saved


# ---------------------------------------------------------------------------
# list_saved_searches
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListSavedSearchesQuery:
    """The input to ``list_saved_searches.handle``."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    limit: int = DEFAULT_LIST_PAGE_LIMIT
    offset: int = 0


def _validate_list_query(q: ListSavedSearchesQuery) -> None:
    if q.limit < 1 or q.limit > MAX_LIST_PAGE_LIMIT:
        raise ValidationFailed(f"limit must be in [1, {MAX_LIST_PAGE_LIMIT}]", field="limit")
    if q.offset < 0:
        raise ValidationFailed("offset must be >= 0", field="offset")


async def list_saved_searches(
    query: ListSavedSearchesQuery,
    *,
    repository: SavedSearchRepository,
) -> list[SavedSearch]:
    """Return the actor's saved-search rows, newest first.

    Owner-scoped: only the actor's own saved searches are returned
    (no cross-actor visibility). The use case does NOT emit an audit
    row — listing is a UI affordance, not a search activity. A
    security administrator's report is built by ``view_search_audit``
    instead.
    """
    _validate_list_query(query)
    return list(
        await repository.list_for_owner(
            query.tenant_id, query.actor_id, limit=query.limit, offset=query.offset
        )
    )


# ---------------------------------------------------------------------------
# delete_saved_search
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeleteSavedSearchCommand:
    """The input to ``delete_saved_search.handle``."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    saved_search_id: UUID


async def delete_saved_search(
    cmd: DeleteSavedSearchCommand,
    *,
    repository: SavedSearchRepository,
) -> None:
    """Delete the actor's saved-search row.

    Owner-only: the use case re-fetches the row through the
    tenant-scoped ``get`` and rejects any row whose ``owner_user_id``
    does not match ``cmd.actor_id``. The repository's tenant filter
    still runs (``WHERE tenant_id = :tenant_id``) so a cross-tenant
    id is silently ignored. The delete does NOT emit an audit row;
    the save itself is the audited action.
    """
    actor = UserContext(
        user_id=cmd.actor_id,
        tenant_id=cmd.tenant_id,
        display_name="",
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.CASE_READ,
        actor,
        Resource(tenant_id=cmd.tenant_id, kind="saved_search"),
    )
    existing = await repository.get(cmd.tenant_id, cmd.saved_search_id)
    if existing is None:
        # Cross-tenant id and missing id collapse to ``NotFound`` so
        # the API never confirms cross-tenant existence (Constitution V).
        raise NotFound(f"saved search {cmd.saved_search_id} not found")
    if existing.owner_user_id != cmd.actor_id:
        # Refuse the delete; a different owner's row is invisible to
        # the actor (we never reveal that it exists).
        raise NotFound(f"saved search {cmd.saved_search_id} not found")
    deleted = await repository.delete(cmd.tenant_id, cmd.saved_search_id)
    if not deleted:
        # The repository raced with another deleter; surface as 404
        # so the client knows the row is gone.
        raise NotFound(f"saved search {cmd.saved_search_id} not found")


# ---------------------------------------------------------------------------
# run_saved_search
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunSavedSearchCommand:
    """The input to ``run_saved_search.handle``."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    saved_search_id: UUID
    searchable_field_keys: tuple[str, ...] = ()
    sensitive_field_keys: tuple[str, ...] = ()
    limit: int = 50
    offset: int = 0


def _validate_run_command(cmd: RunSavedSearchCommand) -> None:
    if cmd.limit < 1 or cmd.limit > 200:
        raise ValidationFailed("limit must be in [1, 200]", field="limit")
    if cmd.offset < 0:
        raise ValidationFailed("offset must be >= 0", field="offset")


async def run_saved_search(
    cmd: RunSavedSearchCommand,
    *,
    repository: SavedSearchRepository,
    audit: SavedSearchAuditRepository,
    search_provider: SearchProvider,
    clock: Clock,
) -> SearchResultPage:
    """Re-execute a previously saved search and audit the execution.

    The execution path goes through the same ``SearchProvider`` the
    live case search uses, so a saved search's results match what the
    actor would have seen had they typed the same query by hand. The
    audit row carries the canonical query shape, the timestamp, and
    the result count (P06-US6 / FM-032).

    The use case does NOT short-circuit when the saved-search row is
    missing: a missing id returns ``NotFound`` so the API can surface
    a 404 without leaking cross-tenant existence.
    """
    _validate_run_command(cmd)
    actor = UserContext(
        user_id=cmd.actor_id,
        tenant_id=cmd.tenant_id,
        display_name="",
        roles=cmd.actor_roles,
    )
    await authorize(
        Action.CASE_READ,
        actor,
        Resource(tenant_id=cmd.tenant_id, kind="saved_search"),
    )
    saved = await repository.get(cmd.tenant_id, cmd.saved_search_id)
    if saved is None:
        raise NotFound(f"saved search {cmd.saved_search_id} not found")
    # Owner-only reuse keeps the "my saved searches" contract clean:
    # a worker cannot run a colleague's saved search (the audit
    # surface would then confuse "who ran this" with "who saved this").
    if saved.owner_user_id != cmd.actor_id:
        raise NotFound(f"saved search {cmd.saved_search_id} not found")

    # Re-execute via the live case-search use case. The searchable /
    # sensitive field-key sets are pre-resolved by the router from the
    # tenant's published CaseTypeVersions; the use case consumes them
    # as sealed inputs so the SQL adapter can project redacted snippets
    # without re-reading the schema.
    port_filters = tuple(SearchFilter(field=f[0], op=f[1], value=f[2]) for f in saved.filters)
    spec = SearchSpec(
        tenant_id=cmd.tenant_id,
        text_query=saved.text_query,
        filters=port_filters,
        limit=cmd.limit,
        offset=cmd.offset,
        can_see_sensitive=False,
        searchable_field_keys=cmd.searchable_field_keys,
        sensitive_field_keys=cmd.sensitive_field_keys,
    )
    # Translate to the application query; the case-search use case
    # re-authorizes against CASE_READ so the saved-search use case is
    # belt-and-suspenders.
    result = await _default_execute(
        CaseSearchQuery(
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            actor_roles=cmd.actor_roles,
            text_query=saved.text_query,
            filters=saved.filters,
            limit=cmd.limit,
            offset=cmd.offset,
            searchable_field_keys=cmd.searchable_field_keys,
            sensitive_field_keys=cmd.sensitive_field_keys,
        ),
        search_provider=search_provider,
    )

    now = clock.now()
    await audit.append(
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        saved_search_id=saved.id,
        query_shape=saved.query_shape,
        result_count=result.total_considered,
        occurred_at=now,
    )
    # ``spec`` is built above for completeness; the live execution
    # above uses the application query directly so the SQL adapter
    # always sees the same shape the live search path builds.
    _ = spec
    return result


# ---------------------------------------------------------------------------
# view_search_audit (security staff surface, P06-US5 / FM-032)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ViewSearchAuditQuery:
    """The input to ``view_search_audit.handle``."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    actor_filter: UUID | None = None
    saved_search_id: UUID | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = DEFAULT_AUDIT_PAGE_LIMIT
    offset: int = 0


def _validate_audit_query(q: ViewSearchAuditQuery) -> None:
    if q.limit < 1 or q.limit > MAX_AUDIT_PAGE_LIMIT:
        raise ValidationFailed(f"limit must be in [1, {MAX_AUDIT_PAGE_LIMIT}]", field="limit")
    if q.offset < 0:
        raise ValidationFailed("offset must be >= 0", field="offset")
    if q.since is not None and q.until is not None and q.since > q.until:
        raise ValidationFailed("since must be <= until", field="since")


@dataclass(frozen=True, slots=True)
class SearchAuditEntry:
    """A single search-audit row on the security-report wire shape."""

    id: UUID
    occurred_at: str
    actor_id: UUID
    saved_search_id: UUID | None
    query_shape: dict[str, Any]
    result_count: int


@dataclass(frozen=True, slots=True)
class SearchAuditPage:
    """A page of search-audit rows plus the standard cursor envelope."""

    items: list[SearchAuditEntry]
    limit: int


def _row_to_entry(row: Any) -> SearchAuditEntry:
    """Project a persistence row onto the wire ``SearchAuditEntry``.

    Mirrors :func:`app.application.audit.access_audit._row_to_entry`:
    one helper per wire shape so a future "decision column" addition
    cannot drift between routers and tests.
    """
    occurred_at = row.occurred_at
    return SearchAuditEntry(
        id=row.id,
        occurred_at=occurred_at.isoformat()
        if hasattr(occurred_at, "isoformat")
        else str(occurred_at),
        actor_id=row.actor_id,
        saved_search_id=getattr(row, "saved_search_id", None),
        query_shape=dict(getattr(row, "query_shape", {}) or {}),
        result_count=int(getattr(row, "result_count", 0) or 0),
    )


async def view_search_audit(
    query: ViewSearchAuditQuery,
    *,
    audit: SavedSearchAuditRepository,
) -> SearchAuditPage:
    """Return a page of saved-search audit rows (P06-US5 / FM-032).

    The actor must hold ``Action.AUDIT_READ``. The use case re-
    evaluates the policy so a misconfigured router cannot leak audit
    rows to a non-security actor. The query shape and the result count
    are returned verbatim so the security report can answer "who
    searched for what and saw how many results".
    """
    _validate_audit_query(query)
    actor = UserContext(
        user_id=query.actor_id,
        tenant_id=query.tenant_id,
        display_name="",
        roles=query.actor_roles,
    )
    await authorize(
        Action.AUDIT_READ,
        actor,
        Resource(tenant_id=query.tenant_id, kind="search_audit_report"),
    )
    rows = await audit.list_for_security_report(
        tenant_id=query.tenant_id,
        actor_id=query.actor_filter,
        saved_search_id=query.saved_search_id,
        since=query.since,
        until=query.until,
        limit=query.limit,
        offset=query.offset,
    )
    items = [_row_to_entry(r) for r in rows]
    return SearchAuditPage(items=items, limit=query.limit)


__all__ = [
    "DEFAULT_AUDIT_PAGE_LIMIT",
    "DEFAULT_LIST_PAGE_LIMIT",
    "DeleteSavedSearchCommand",
    "ListSavedSearchesQuery",
    "MAX_AUDIT_PAGE_LIMIT",
    "MAX_LIST_PAGE_LIMIT",
    "RunSavedSearchCommand",
    "SaveSearchCommand",
    "SearchAuditEntry",
    "SearchAuditPage",
    "ViewSearchAuditQuery",
    "delete_saved_search",
    "list_saved_searches",
    "run_saved_search",
    "save_search",
    "view_search_audit",
]
