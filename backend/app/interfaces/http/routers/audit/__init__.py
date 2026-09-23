"""Audit router package: timeline, export, correction, access-decisions, signed batches.

# INTEGRATION_SEAM: HTTP entry

Formerly a single ``audit.py`` module (747 lines) — past the R2
600-line soft limit. This package is the only HTTP entry to the
per-case audit timeline and the audit read/write/report surface. Split
by route-group responsibility into :mod:`common` (shared ``router``,
ISO-8601 wire timestamp parser, timeline entry projection),
:mod:`timeline` (``GET /api/v1/cases/{case_id}/timeline``),
:mod:`export` (``GET /api/v1/audit/export``), :mod:`correction`
(``POST /api/v1/audit/{event_id}/correction``), :mod:`access_decisions`
(``GET /api/v1/audit/access-decisions``), and :mod:`signed_batches`
(``GET/POST /api/v1/audit/signed-batches``). This ``__init__`` is a
pure re-export shim: ``__all__`` and every previously visible
attribute — public and private, including names the old module merely
imported, such as ``asdict``/``datetime``/``UTC``/``status`` — are
re-exported unchanged, so no consumer import line needs editing
(``from ...audit import router`` in ``app/main.py:152`` and
``tests/interfaces/test_timeline.py:65`` keeps working, as does
``mock.patch("app.interfaces.http.routers.audit.X")``). The route set,
route order, and OpenAPI operation ids are unchanged.

Every endpoint is authz-checked and tenant-scoped, and delegates the
business decision to the application layer under
``app.application.audit``. The router's job is limited to parsing and
clamping the wire input, building the application-layer query/command,
mapping results onto the Pydantic wire schemas (the OpenAPI contract —
hand-editing fields at the router is forbidden, AGENTS.md §4.R7), and
translating domain errors via the existing handler
(``interfaces/http/errors.py``).

Endpoints:

- ``GET /api/v1/cases/{case_id}/timeline`` — paginated, redacted,
  chronologically-ordered audit events for one case (P09-US1 / US4 /
  FM-043 / FR-022). Authorization: ``Action.AUDIT_READ``; cross-tenant
  ids surface as 404 so the actor cannot probe (Constitution V).
- ``GET /api/v1/audit/export`` — structured export where every row
  carries ``hash``/``prev_hash`` so a downstream auditor can verify
  the chain off-system (P09-US5 / FM-044). The export appends an
  ``AuditExported`` row so the action is visible on the timeline.
- ``POST /api/v1/audit/{event_id}/correction`` — append a compensating
  ``AuditCorrectionRecorded`` row; the original event is never
  modified (P09-US7 / FM-044).
- ``GET /api/v1/audit/access-decisions`` — security report of allowed
  and denied access decisions (P09-US6 / FM-045; P11-US9).
- ``GET /api/v1/audit/signed-batches`` and
  ``POST /api/v1/audit/signed-batches`` — externally-verifiable signed
  batch surface (P09-US9 / FM-046; ADR-009 Gated). The route resolves
  the per-request ``SignedBatchRepository`` from ``app.state`` so a
  production deployment can swap in the Azure tamper-evident blob
  store without code changes (the application port stays the same).
"""

from __future__ import annotations

from .access_decisions import (  # noqa: F401
    AccessDecisionPage,
    AccessDecisionResponse,
    Action,
    Clock,
    CorrelationContext,
    Resource,
    access_audit_uc,
    authorize,
    get_access_decisions,
    get_clock,
    get_correlation_id,
)
from .common import (  # noqa: F401
    APIRouter,
    TimelineEntryResponse,
    ValidationFailed,
    _entry_to_response,
    _iso_to_naive_utc,
    asdict,
    datetime,
    router,
    view_timeline_uc,
)
from .correction import (  # noqa: F401
    UTC,
    Body,
    CorrectionRequest,
    CorrectionResponse,
    correct_audit_uc,
    post_audit_correction,
)
from .export import (  # noqa: F401
    ExportBatchResponse,
    ExportEntryResponse,
    export_audit_uc,
    get_audit_export,
)
from .signed_batches import (  # noqa: F401
    Request,
    SignedBatchPage,
    SignedBatchRequest,
    SignedBatchResponse,
    _batch_to_response,
    get_signed_batch_repository,
    get_signed_batches,
    post_signed_batch,
    siem_stream_uc,
)
from .timeline import (  # noqa: F401
    ALL_EVENT_TYPES,
    UUID,
    Annotated,
    AsyncSession,
    AuditHttpDependencyProvider,
    Depends,
    HTTPException,
    NotFound,
    Query,
    TimelinePage,
    UserContext,
    get_audit_dependencies,
    get_case_timeline,
    get_current_user,
    get_db,
    status,
)

__all__ = ["router"]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, and packages additionally carry ``__path__`` — neither
# existed on the pre-split single-file module. Popping them keeps
# ``dir(app.interfaces.http.routers.audit)`` byte-identical to the old
# module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (``...audit.timeline`` resolves through
# ``sys.modules``, which is untouched); only the redundant package-level
# attributes go away.
for _submodule in (
    "access_decisions",
    "common",
    "correction",
    "export",
    "signed_batches",
    "timeline",
    "__path__",
):
    globals().pop(_submodule, None)
del _submodule
