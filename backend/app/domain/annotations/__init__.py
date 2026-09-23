"""Viewer annotation, redaction-rendition, checkout, and edit-lock rules (T5 / FM-023 ext, FM-025).

The T5 viewer paragraph (``specs/002-leftovers/spec.md`` §3 T5) requires a
browser-native viewer pipeline: paged/thumbnail/detail modes, an annotation
layer (highlight / text-box / stamp / freehand), redaction burn as a new
rendition, revision/rendition history, persistent checkout with history,
concurrent-edit locking, and a re-index action. Audio/video playback stays
behind an adapter and is out of scope for this package.

# Why a package (R2 structural split)

Formerly a single ``annotations.py`` module of 906 lines (768 non-blank) —
past the R2 600-line soft limit. Split by responsibility into
:mod:`constants` (bounded limits + the four viewer enums),
:mod:`helpers` (private geometry/kind/mode/lease/storage-ref validators),
:mod:`annotation` (the ``Annotation`` overlay aggregate),
:mod:`rendition` (the ``Rendition`` descriptor, the read-side resolver,
and ``PermissionDenied``), :mod:`redaction` (propose/apply/burn marks),
:mod:`checkout` (``CheckoutLock`` + ``CheckoutConflict``),
:mod:`edit_lock` (the pure concurrent-edit rule), and :mod:`reindex`
(the re-index value object). This ``__init__`` is a pure re-export shim:
``__all__`` and every previously visible attribute — including the
private ``_coerce_*`` / ``_validate_*`` helpers and the stdlib/error
names (``UUID``, ``datetime``, ``dataclass``, ``Conflict``,
``InvariantViolation``, ``ValidationFailed``, ...) — are re-exported
unchanged, so no consumer import line and no
``mock.patch("app.domain.annotations.…")`` target needs editing.

# What lives here vs elsewhere

- :class:`Annotation` — viewer markup pinned to ``(document_id,
  document_version, page)``. Annotations never mutate document bytes; they
  are an overlay layer the viewer renders. Content edits go through the
  ``Document`` aggregate (``app.domain.document``).
- :class:`Redaction` — a viewer-proposed redaction mark (page + region +
  reason). Burning an approved mark produces a :class:`Rendition` of kind
  ``REDACTED`` whose bytes are written through ``DocumentStore.put`` by the
  application layer. The original bytes and the ``DocumentVersion`` rows
  stay untouched so the original remains retrievable under a stricter
  right (T5 acceptance: "original retrievable under stricter right; both
  audited"). This package only shapes the mark and the rendition
  descriptor; it never touches bytes.
- :class:`CheckoutLock` — a persistent exclusive checkout of a document
  for editing. At most one active checkout per document. A conflicting
  acquire raises :class:`CheckoutConflict`, which carries the current
  holder plus expiry so the HTTP boundary can render a structured
  ``conflict`` envelope (T5 acceptance: "checkout/lock conflicts return
  structured ``conflict`` with holder + expiry").
- :func:`evaluate_edit_lock` — the pure concurrent-edit rule: who may
  mutate document metadata/bytes given the active checkout and the clock.
- :class:`Rendition` + :func:`resolve_viewer_rendition` — rendition rules
  (original vs redacted vs thumbnail/preview) and the read-side guard
  that decides which rendition a viewer mode may serve.
- :class:`ReindexRequest` — the viewer "re-index" action as a validated
  value object; the actual index rebuild lives behind a search-adapter
  seam and is out of scope here.

# Why pure domain (no I/O, no vendor SDK)

Per AGENTS.md R11 and the T3 signature-seam rule, the domain must work
without any vendor SDK (no DocuSign/Adobe, no viewer component library,
no storage client). All time comes from an explicit ``now`` argument;
all persistence (checkout rows, annotation rows, rendition bytes) is the
repository/adapter's job. The application layer threads ``now`` from the
``Clock`` port so tests pin a fixed instant.

# INTEGRATION_SEAM: viewer + DocumentStore + search index

- Bytes: ``DocumentStore`` port (``app.domain.ports``); rendition
  ``storage_ref`` values are opaque strings minted by the adapter.
- Playback (audio/video): a future ``MediaPlayback`` adapter; the domain
  only records ``content_type`` so the viewer can pick a player.
- Re-index: a future search-index adapter; this package validates the
  request, the adapter performs the rebuild.
"""

from __future__ import annotations

from .annotation import (  # noqa: F401
    UUID,
    Annotation,
    dataclass,
    datetime,
    uuid4,
)
from .checkout import (  # noqa: F401
    CheckoutConflict,
    CheckoutLock,
    Conflict,
    InvariantViolation,
    replace,
    timedelta,
)
from .constants import (  # noqa: F401
    ANNOTATION_KINDS,
    DEFAULT_CHECKOUT_LEASE_SECONDS,
    MAX_CHECKOUT_LEASE_SECONDS,
    MAX_GEOMETRY_KEYS,
    MAX_STAMP_LENGTH,
    MAX_TEXT_LENGTH,
    VIEWER_MODES,
    AnnotationKind,
    Enum,
    RedactionStatus,
    RenditionKind,
    ViewerMode,
)
from .edit_lock import (  # noqa: F401
    EditLockDecision,
    evaluate_edit_lock,
)
from .helpers import (  # noqa: F401
    ValidationFailed,
    _coerce_annotation_kind,
    _coerce_viewer_mode,
    _validate_annotation_content,
    _validate_geometry,
    _validate_lease,
    _validate_storage_ref,
)
from .redaction import Redaction  # noqa: F401
from .reindex import ReindexRequest  # noqa: F401
from .rendition import (  # noqa: F401
    PermissionDenied,
    Rendition,
    resolve_viewer_rendition,
)

__all__ = [
    "ANNOTATION_KINDS",
    "DEFAULT_CHECKOUT_LEASE_SECONDS",
    "MAX_CHECKOUT_LEASE_SECONDS",
    "MAX_GEOMETRY_KEYS",
    "MAX_STAMP_LENGTH",
    "MAX_TEXT_LENGTH",
    "VIEWER_MODES",
    "Annotation",
    "AnnotationKind",
    "CheckoutConflict",
    "CheckoutLock",
    "EditLockDecision",
    "PermissionDenied",
    "Redaction",
    "RedactionStatus",
    "ReindexRequest",
    "Rendition",
    "RenditionKind",
    "ViewerMode",
    "evaluate_edit_lock",
    "resolve_viewer_rendition",
]

# why this loop: importing the submodules above bound each one as an attribute
# of this package, and packages also expose ``__path__`` — neither existed on
# the pre-split single-file module. Popping them keeps
# ``dir(app.domain.annotations)`` byte-identical to the old module's attribute
# surface: the R2 refactor must be invisible, and the dir() gate diffs names
# exactly. Every submodule is eagerly imported above, so later resolves
# (``from app.domain.annotations import X``, ``mock.patch("app.domain.annotations.X")``,
# ``import app.domain.annotations.<sub>``) all hit ``sys.modules`` directly and
# never need the package-level attributes back.
for _submodule in (
    "__path__",
    "annotation",
    "checkout",
    "constants",
    "edit_lock",
    "helpers",
    "redaction",
    "rendition",
    "reindex",
):
    globals().pop(_submodule, None)
del _submodule
