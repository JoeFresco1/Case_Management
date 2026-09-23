"""Case search router (P06-US1, US2, US3, US5, US6, US7 / FM-031, FM-032, FM-034).

Exposes ad-hoc search endpoints that share the same use case:

- ``GET /api/v1/cases/search`` — query string + filter parameters
  (``?query=...&status=open&priority=urgent&limit=50``). Convenient for
  browser navigation and linkable URLs.
- ``GET /api/v1/search/cases`` — alias of the above on the plan
  contract path (``specs/002-leftovers/plan.md §4``). Same parameters,
  same response, same audit row.
- ``POST /api/v1/search`` — JSON body with the same shape. Convenient
  for richer clients (TanStack Query, scripted admin tools).
- ``POST /api/v1/search/semantic`` — same body, AI-ranked by the
  ``SearchRanker`` port (P06-US7 / FM-034); the ranker reorders but
  never widens the authorized candidate set.
- ``POST/GET/DELETE /api/v1/search/saved[...]`` plus the ``/run``
  action — reusable, owner-scoped queries (P06-US3 / FM-032).
- ``GET /api/v1/search_audit`` — security-staff report over save + run
  events (P06-US5 / FM-032).

Every ad-hoc execution appends one ``search_audit`` row carrying the
actor, the canonical PHI-free query shape, the timestamp, and the
result count (``saved_search_id=None``); the row is committed in the
request transaction so ``GET /api/v1/search_audit`` answers "who
searched for what and saw how many results" for ad-hoc and saved
searches alike (P06-US6 / FM-032).

All endpoints delegate to the application layer
(``app.application.search.case_search`` / ``semantic_search`` /
``saved_searches``) which authorizes the actor, validates the filter
shape against the published CaseTypeVersion's declared-searchable
keys, and delegates the actual execution to ``SqlSearchProvider``. The
router only translates between the wire shape and the application
shape; no business logic lives here.

# Why a package (R2 structural split)

Formerly a single ``search.py`` module of 925 lines — past the R2
600-line soft limit. Split by route-group responsibility into
:mod:`common` (shared ``router``, field-key resolution, wire
projections, ad-hoc audit row, shared execution), :mod:`adhoc` (the
three ad-hoc spellings), :mod:`semantic` (AI-ranked search),
:mod:`saved` (saved-search CRUD + run), and :mod:`audit` (the
security-staff audit report). This ``__init__`` is a pure re-export
shim: ``__all__`` and every previously visible attribute — public and
private, including names the old module merely imported, such as
``datetime``/``status``/``Annotated`` — are re-exported unchanged, so
no consumer import line needs editing
(``from ...search import router`` and
``mock.patch("app.interfaces.http.routers.search.X")`` keep working).
Route registration order across the route modules is irrelevant: no
two search paths overlap at the same segment depth and method, so
Starlette's first-match rule resolves identically regardless of import
order.

# INTEGRATION_SEAM: search engine

The router resolves the searchable / sensitive field-key sets by reading
the tenant's published CaseTypeVersions through the same repositories
the case-creation flow already uses. The two sets are the P05 contract
(the schema is the source of truth for what is queryable / sensitive)
and are passed into the use case as plain tuples so the application
layer stays pure. The ``SearchRanker`` AI port (semantic search) is a
second seam reached through the session dependency bundle — see
:mod:`semantic`.
"""

from __future__ import annotations

from .adhoc import (  # noqa: F401
    MAX_LIMIT,
    Annotated,
    CaseSearchRequest,
    Depends,
    Query,
    SearchHttpDependencyProvider,
    SystemClock,
    get_clock,
    get_current_user,
    get_db,
    get_search,
    get_search_cases,
    get_search_dependencies,
    post_search,
)
from .audit import (  # noqa: F401
    SearchAuditEntryResponse,
    SearchAuditList,
    datetime,
    get_search_audit,
)
from .common import (  # noqa: F401
    UUID,
    Any,
    APIRouter,
    AsyncSession,
    CaseSearchHit,
    CaseSearchResponse,
    Clock,
    HTTPException,
    QueryFilter,
    SearchFieldKeyResolver,
    SearchFilterRequest,
    SearchHttpRepositories,
    SearchQuery,
    UserContext,
    ValidationFailed,
    _adhoc_audit_shape,
    _audit_adhoc_search,
    _hit_to_wire,
    _resolve_field_keys,
    _run_search,
    _validate_filter_request,
    case_search,
    query_shape_for_audit,
    router,
    status,
)
from .saved import (  # noqa: F401
    Conflict,
    NotFound,
    Response,
    SavedSearchList,
    SavedSearchRequest,
    SavedSearchResponse,
    SortEntry,
    SortSpec,
    _saved_to_wire,
    delete_saved_search,
    list_saved_searches,
    post_saved_search,
    run_saved_search,
    saved_searches_uc,
)
from .semantic import (  # noqa: F401
    CorrelationContext,
    _run_semantic_search,
    get_correlation_id,
    post_semantic_search,
    semantic_search,
)

__all__ = ["router"]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, and packages additionally carry ``__path__`` — neither
# existed on the pre-split single-file module. Popping them keeps
# ``dir(app.interfaces.http.routers.search)`` byte-identical to the old
# module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (``...search.common`` resolves through
# ``sys.modules``, which is untouched); only the redundant package-level
# attributes go away. ``__path__`` alone is tolerated to remain per the
# ``application/webhooks/outbound`` precedent; popping it matches
# ``routers/retention``.
for _submodule in (
    "adhoc",
    "audit",
    "common",
    "saved",
    "semantic",
    "__path__",
):
    globals().pop(_submodule, None)
del _submodule
