"""Viewer vocabulary: bounded limits and overlay/rendition enums (T5 / FM-023 ext).

Constitution X — boring, greppable limits shared by every submodule of
:mod:`app.domain.annotations`, plus the four ``str, Enum`` vocabularies that
define the viewer pipeline: the annotation-layer kinds, the redaction mark
lifecycle, the rendition kinds (original / redacted / thumbnail / preview),
and the browser-native viewer modes.

Pure data only: no I/O, no vendor SDK (R11). Every limit is a module-level
constant so a reviewer can grep one file for the whole envelope instead of
hunting magic numbers across aggregates.

Symbols: ``MAX_TEXT_LENGTH``, ``MAX_STAMP_LENGTH``, ``MAX_GEOMETRY_KEYS``,
``DEFAULT_CHECKOUT_LEASE_SECONDS``, ``MAX_CHECKOUT_LEASE_SECONDS``,
``ANNOTATION_KINDS``, ``VIEWER_MODES``, ``AnnotationKind``,
``RedactionStatus``, ``RenditionKind``, ``ViewerMode``.
"""

from __future__ import annotations

from enum import Enum

#: Maximum chars for free-text annotation content / redaction reason.
MAX_TEXT_LENGTH = 500

#: Maximum chars for a stamp label.
MAX_STAMP_LENGTH = 120

#: Maximum keys allowed in an annotation geometry mapping (bounds + style).
MAX_GEOMETRY_KEYS = 16

#: Default checkout lease in seconds (4 hours) when the caller passes none.
DEFAULT_CHECKOUT_LEASE_SECONDS = 4 * 60 * 60

#: Maximum checkout lease in seconds (7 days); bounds a runaway lock.
MAX_CHECKOUT_LEASE_SECONDS = 7 * 24 * 60 * 60

#: Allowed annotation kinds — the viewer overlay vocabulary (T5).
ANNOTATION_KINDS = frozenset({"highlight", "text_box", "stamp", "freehand"})

#: Viewer modes the rendition resolver understands (T5 pipeline).
VIEWER_MODES = frozenset({"paged", "thumbnail", "detail"})


class AnnotationKind(str, Enum):  # noqa: UP042
    """The viewer overlay vocabulary (T5 annotation layer)."""

    HIGHLIGHT = "highlight"
    TEXT_BOX = "text_box"
    STAMP = "stamp"
    FREEHAND = "freehand"


class RedactionStatus(str, Enum):  # noqa: UP042
    """Lifecycle of a viewer redaction mark."""

    PROPOSED = "proposed"
    APPLIED = "applied"
    SUPERSEDED = "superseded"


class RenditionKind(str, Enum):  # noqa: UP042
    """The rendition vocabulary for the viewer pipeline.

    - ``ORIGINAL``: the filed bytes of a ``DocumentVersion``.
    - ``REDACTED``: bytes produced by burning one or more applied
      redaction marks; stored as a *new* rendition, never overwriting.
    - ``THUMBNAIL`` / ``PREVIEW``: derived low-fidelity renditions for
      the thumbnail/detail viewer modes.
    """

    ORIGINAL = "original"
    REDACTED = "redacted"
    THUMBNAIL = "thumbnail"
    PREVIEW = "preview"


class ViewerMode(str, Enum):  # noqa: UP042
    """Browser-native viewer modes (T5 viewer pipeline)."""

    PAGED = "paged"
    THUMBNAIL = "thumbnail"
    DETAIL = "detail"


__all__ = [
    "ANNOTATION_KINDS",
    "DEFAULT_CHECKOUT_LEASE_SECONDS",
    "MAX_CHECKOUT_LEASE_SECONDS",
    "MAX_GEOMETRY_KEYS",
    "MAX_STAMP_LENGTH",
    "MAX_TEXT_LENGTH",
    "VIEWER_MODES",
    "AnnotationKind",
    "Enum",
    "RedactionStatus",
    "RenditionKind",
    "ViewerMode",
]
