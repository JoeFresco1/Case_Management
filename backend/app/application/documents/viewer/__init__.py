"""Viewer-pipeline use cases (T5 / FM-023 ext): annotations, checkout, renditions, re-index.

# Why a package (R2 structural split)

Formerly a single ``viewer.py`` module of 880 lines — past the R2
600-line soft limit. Split by responsibility into :mod:`events`,
:mod:`helpers`, :mod:`annotation_ops`, :mod:`checkout_ops`,
:mod:`rendition_ops`, and :mod:`reindex_ops`. This ``__init__`` is a
pure re-export shim: ``__all__`` and every previously visible
attribute are re-exported unchanged, so no consumer import line needs
editing (HTTP router ``app.interfaces.http.routers.viewer`` and
``tests/application/documents/test_viewer.py`` included).

# Overlay model

The browser-native viewer renders documents in paged/thumbnail/detail
modes over an **overlay** model:

- **Annotations** (highlight / text-box / stamp / freehand) are markup
  pinned to ``(document_id, document_version, page)``. They never
  mutate document bytes: the annotation path touches only the
  ``viewer_annotations`` rows plus audit + outbox. The ``documents`` /
  ``document_versions`` rows are not read for content and never
  written here (T5 acceptance: "annotation layer never mutates
  original bytes").
- **Checkout / checkin** is a persistent exclusive lock with holder +
  expiry. A conflicting acquire raises ``CheckoutConflict`` carrying
  holder + expiry so the HTTP boundary renders a structured
  ``conflict`` envelope (T5 acceptance).
- **Renditions** are burned viewer bytes (redacted burns) stored via
  ``DocumentStore.put`` as *new* content; the original version rows
  stay untouched so the original remains retrievable.
- **Re-index** validates *what* to rebuild and *why*; the rebuild
  itself runs behind the search-index seam (out of scope), audited as
  ``DocumentReindexed``.

# INTEGRATION_SEAM: search index

``request_reindex`` records the validated request + audit + outbox.
A future search-index adapter subscribes to the ``DocumentReindexed``
outbox event to rebuild the full-text/metadata catalog. The portable
``LIKE`` provider needs no rebuild, so local behavior is already
convergent; the event exists so Azure Cognitive Search can converge
later without a contract change.

# Failure semantics

- Auth / not-found / validation errors raise before any write.
- ``CheckoutConflict`` raises before any write (holder + expiry in
  ``to_details()``).
- On ``DocumentStore.put`` raising ``DependencyUnavailable`` the caller
  rolls the transaction back; no audit row is appended.
"""

from __future__ import annotations

from .annotation_ops import (  # noqa: F401
    Action,
    Annotation,
    AnnotationKind,
    AnnotationRepository,
    CreateAnnotationCommand,
    DeleteAnnotationCommand,
    Resource,
    UpdateAnnotationCommand,
    authorize,
    create_annotation,
    dataclass,
    delete_annotation,
    field,
    replace,
    update_annotation,
)
from .checkout_ops import (  # noqa: F401
    CheckinCommand,
    CheckoutCommand,
    CheckoutConflict,
    CheckoutLock,
    CheckoutLockRepository,
    CheckoutResult,
    checkin_document,
    checkout_document,
)
from .events import (  # noqa: F401
    ANNOTATION_CREATED,
    ANNOTATION_DELETED,
    ANNOTATION_UPDATED,
    DOCUMENT_CHECKED_IN,
    DOCUMENT_CHECKED_OUT,
    DOCUMENT_CHECKOUT_RENEWED,
    DOCUMENT_REINDEXED,
    RENDITION_BURNED,
)
from .helpers import (  # noqa: F401
    SUBJECT_TYPE_DOCUMENT,
    UUID,
    Any,
    AuditEventStore,
    DocumentRepository,
    InvariantViolation,
    NotFound,
    OutboxWriter,
    UserContext,
    _actor,
    _append_audit_and_outbox,
    _new_correlation_id,
    _resolve_document,
    uuid4,
)
from .reindex_ops import (  # noqa: F401
    ReindexCommand,
    ReindexRequest,
    request_reindex,
)
from .rendition_ops import (  # noqa: F401
    BurnRenditionCommand,
    DocumentStore,
    DocumentVersion,
    Rendition,
    RenditionKind,
    RenditionRepository,
    RevisionEntry,
    RevisionHistory,
    burn_rendition,
    revision_history,
)

# Re-exported so callers can catch the structured conflict without
# importing the domain package directly.
__all__ = [
    "ANNOTATION_CREATED",
    "ANNOTATION_DELETED",
    "ANNOTATION_UPDATED",
    "DOCUMENT_CHECKED_IN",
    "DOCUMENT_CHECKED_OUT",
    "DOCUMENT_CHECKOUT_RENEWED",
    "DOCUMENT_REINDEXED",
    "RENDITION_BURNED",
    "AnnotationKind",
    "BurnRenditionCommand",
    "CheckinCommand",
    "CheckoutCommand",
    "CheckoutConflict",
    "CheckoutResult",
    "CreateAnnotationCommand",
    "DeleteAnnotationCommand",
    "InvariantViolation",
    "ReindexCommand",
    "RevisionEntry",
    "RevisionHistory",
    "UpdateAnnotationCommand",
    "burn_rendition",
    "checkin_document",
    "checkout_document",
    "create_annotation",
    "delete_annotation",
    "request_reindex",
    "revision_history",
    "update_annotation",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, but the pre-split single-file module exposed no such
# attributes. Popping them keeps ``dir(app.application.documents.viewer)``
# identical to the old module's attribute surface (``__path__`` aside) — the
# R2 refactor must be invisible. The submodules stay importable
# (``...viewer.helpers`` resolves through ``sys.modules``, which is untouched);
# only the redundant package-level attributes go away.
for _submodule in (
    "annotation_ops",
    "checkout_ops",
    "events",
    "helpers",
    "reindex_ops",
    "rendition_ops",
):
    globals().pop(_submodule, None)
del _submodule
