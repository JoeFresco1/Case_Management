"""Documents router package: upload, content, versions, and protection routes.

# INTEGRATION_SEAM: HTTP entry

Formerly a single ``documents.py`` module (872 lines) — past the R2
600-line soft limit. Split by route-group responsibility into
:mod:`common` (shared ``router`` / ``versions_router``,
``get_document_store``, header-safety + size helpers), :mod:`upload`
(``POST /api/v1/cases/{case_id}/documents``), :mod:`content`
(``GET /api/v1/documents/{id}/content``), :mod:`versions`
(version add + history), and :mod:`protection` (redact / lock /
restrict + rendition list). This ``__init__`` is a pure re-export
shim: ``__all__`` and every previously visible attribute — public and
private, including names the old module merely imported, such as
``re``/``cast``/``status`` — are re-exported unchanged, so no consumer
import line needs editing (``from ...documents import router``,
``viewer.py``'s ``get_document_store``, and
``mock.patch("app.interfaces.http.routers.documents.X")`` keep
working). The route set, route paths, and OpenAPI operation ids are
unchanged.

# Endpoints

The package accepts a ``multipart/form-data`` upload with text fields
plus the binary ``content`` part. Authorization is enforced inside
the use cases (``Action.DOCUMENT_*``); the routers only translate
between HTTP and the application layer.

- ``POST /api/v1/cases/{case_id}/documents`` — upload a document to
  a case (P03-US1 / FM-022 / FR-005).
- ``GET /api/v1/documents/{document_id}/content`` — download the
  ``original`` or ``redacted`` rendition with safe metadata headers.
- ``POST /api/v1/documents/{document_id}/versions`` — add a new bytes
  revision (P03-US2 / FR-006).
- ``GET /api/v1/documents/{document_id}/versions`` — list the
  immutable version history (P03-US2 read side).
- ``POST /api/v1/documents/{document_id}/redactions`` — create a
  versioned redaction (P03-US6 / FM-025).
- ``GET /api/v1/documents/{document_id}/redactions`` — list redaction
  renditions, oldest first (FM-025).
- ``POST /api/v1/documents/{document_id}/lock`` — refuse further
  versions + redactions (P03-US8 / FM-025); a second lock is 423.
- ``POST /api/v1/documents/{document_id}/restrict`` — raise
  sensitivity to ``restricted`` / ``phi`` (FM-025 / T5).

# Idempotency

Every successful mutation is recorded under the ``Idempotency-Key``
header (AGENTS.md §10) so a retry with the same key + same body
returns the prior response. ``IdempotencyMiddleware`` handles the
replay for every mutating endpoint under ``/api/v1``; each router
still rejects a missing/blank key with 400 before the use case runs.

# Rejection policy (FR-005 acceptance scenario 2)

An upload that violates the case type's policy (disallowed content
type, oversize, disallowed classification) returns 422 with a
structured ``validation_failed`` error and persists a
``DocumentRejected`` audit event. No metadata row, no version row, no
outbox row.
"""

from __future__ import annotations

from .common import (  # noqa: F401
    APIRouter,
    DocumentStore,
    Request,
    _content_disposition,
    _router_max_size,
    _safe_content_type,
    cast,
    get_document_store,
    re,
    router,
    upload_document_uc,
    versions_router,
)
from .content import (  # noqa: F401
    UUID,
    AsyncSession,
    CaseHttpDependencyProvider,
    Clock,
    CorrelationContext,
    DependencyUnavailable,
    Depends,
    HTTPException,
    Query,
    Response,
    UserContext,
    download_document,
    download_document_uc,
    get_case_dependencies,
    get_clock,
    get_correlation_id,
    get_current_user,
    get_db,
    status,
)
from .protection import (  # noqa: F401
    Action,
    DocumentLocked,
    DocumentRedactionResponse,
    File,
    Form,
    Header,
    LockDocumentResponse,
    Resource,
    RestrictDocumentRequest,
    RestrictDocumentResponse,
    UploadFile,
    _authorize,
    list_document_redactions,
    lock_document,
    lock_to_response,
    protect_document_uc,
    redact_document,
    redaction_to_response,
    restrict_document,
    restrict_document_uc,
    restrict_to_response,
)
from .upload import (  # noqa: F401
    Annotated,
    DocumentResponse,
    DocumentSensitivity,
    ValidationFailed,
    document_to_response,
    upload_document,
)
from .versions import (  # noqa: F401
    DocumentVersionResponse,
    add_document_version,
    list_document_versions,
    version_document_uc,
    version_to_response,
)

__all__ = [
    "list_document_redactions",
    "lock_document",
    "redact_document",
    "restrict_document",
    "router",
    "versions_router",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, and packages additionally carry ``__path__`` — neither
# existed on the pre-split single-file module. Popping them keeps
# ``dir(app.interfaces.http.routers.documents)`` byte-identical to the old
# module's attribute surface — the R2 refactor must be invisible. The
# submodules stay importable (``...documents.upload`` resolves through
# ``sys.modules``, which is untouched); only the redundant package-level
# attributes go away.
for _submodule in (
    "common",
    "content",
    "protection",
    "upload",
    "versions",
    "__path__",
):
    globals().pop(_submodule, None)
del _submodule
