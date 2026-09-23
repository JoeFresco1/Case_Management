"""Viewer router package: annotations, revisions/renditions, checkout, re-index.

# INTEGRATION_SEAM: HTTP entry

Formerly a single ``viewer.py`` module (653 lines) — past the R2
600-line soft limit. Split by route-group responsibility into
:mod:`common` (shared ``router`` + ``_require_idempotency_key`` guard),
:mod:`annotation` (overlay CRUD: create / list / update / delete),
:mod:`renditions` (unified revision history + rendition list / burn),
:mod:`checkout` (check-out / check-in / checkout history), and
:mod:`reindex` (``POST .../reindex`` behind the search-index seam).
This ``__init__`` is a pure re-export shim: ``__all__`` and every
previously visible attribute — public and private, including names the
old module merely imported, such as ``status``/``UUID``/``viewer_uc`` —
are re-exported unchanged, so no consumer import line needs editing
(``from ...viewer import router`` in ``app/main.py`` and
``tests/interfaces/test_viewer_router.py`` keeps working, as does
``mock.patch("app.interfaces.http.routers.viewer.X")``). The route
set and OpenAPI operation ids are unchanged.

The viewer pipeline is an overlay model: annotations are markup pinned
to ``(document_id, document_version, page)`` and never mutate document
bytes — the annotation path writes only ``viewer_annotations`` rows
plus audit + outbox. Rendition burns store *new* bytes through the
shared ``DocumentStore`` seam; the original version rows stay
untouched. Re-index records a validated request audited as
``DocumentReindexed``; the rebuild runs behind the search-index seam.

Idempotency: every mutation requires the ``Idempotency-Key`` header
(AGENTS.md §10); the ``IdempotencyMiddleware`` replays successful
responses under ``/api/v1``.

Conflict envelope: a checkout lost to a live foreign lock raises
``CheckoutConflict`` (a ``Conflict``), mapped by
``domain_error_handler`` to 409 with ``details.holder`` +
``details.expires_at`` (T5 acceptance).

Endpoints:

- ``POST/GET /api/v1/documents/{document_id}/annotations`` and
  ``PATCH/DELETE /api/v1/documents/{document_id}/annotations/{annotation_id}``
  — overlay markup CRUD (never touches document bytes).
- ``GET /api/v1/documents/{document_id}/revisions`` — filed versions +
  burned renditions as one oldest-first timeline.
- ``GET/POST /api/v1/documents/{document_id}/renditions`` — list burned
  renditions; burn approved redaction bytes as a new rendition.
- ``POST /api/v1/documents/{document_id}/checkout`` and ``.../checkin`` —
  exclusive editing lease with holder + expiry; foreign lock → 409.
- ``GET /api/v1/documents/{document_id}/checkout/history`` — who held
  the document when (newest first).
- ``POST /api/v1/documents/{document_id}/reindex`` — 202 validated
  search-index rebuild request.
"""

from __future__ import annotations

from .annotation import (  # noqa: F401
    UUID,
    Annotated,
    AnnotationCreateRequest,
    AnnotationResponse,
    AnnotationUpdateRequest,
    AsyncSession,
    Clock,
    Depends,
    Header,
    Query,
    UserContext,
    ViewerHttpDependencyProvider,
    annotation_to_response,
    create_annotation,
    delete_annotation,
    get_clock,
    get_current_user,
    get_db,
    get_viewer_dependencies,
    list_annotations,
    update_annotation,
    viewer_uc,
)
from .checkout import (  # noqa: F401
    CheckoutConflict,
    CheckoutRequest,
    CheckoutResponse,
    checkin_document,
    checkout_document,
    checkout_history,
    lock_to_response,
)
from .common import (  # noqa: F401
    APIRouter,
    HTTPException,
    _require_idempotency_key,
    router,
    status,
)
from .reindex import (  # noqa: F401
    ReindexRequestBody,
    ReindexResponse,
    request_reindex,
)
from .renditions import (  # noqa: F401
    DocumentStore,
    File,
    Form,
    RenditionResponse,
    RevisionEntryResponse,
    RevisionHistoryResponse,
    UploadFile,
    burn_rendition,
    get_document_store,
    get_revisions,
    list_renditions,
    rendition_to_response,
)

__all__ = [
    "burn_rendition",
    "checkin_document",
    "checkout_document",
    "checkout_history",
    "create_annotation",
    "delete_annotation",
    "get_revisions",
    "list_annotations",
    "list_renditions",
    "request_reindex",
    "router",
    "update_annotation",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, and packages additionally carry ``__path__`` — neither
# existed on the pre-split single-file module. Popping them keeps
# ``dir(app.interfaces.http.routers.viewer)`` byte-identical to the old
# module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (``...viewer.annotation`` resolves through
# ``sys.modules``, which is untouched); only the redundant package-level
# attributes go away.
for _submodule in (
    "annotation",
    "checkout",
    "common",
    "reindex",
    "renditions",
    "__path__",
):
    globals().pop(_submodule, None)
del _submodule
