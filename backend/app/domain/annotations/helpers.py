"""Private geometry/kind/mode/lease/storage-ref guards (pure, no I/O).

Small validators referenced by more than one sibling submodule
(:mod:`annotation`, :mod:`rendition`, :mod:`redaction`, :mod:`checkout`,
:mod:`reindex`). Living in this leaf module keeps one implementation per
rule and avoids circular imports between those modules. All guards raise
:class:`~app.domain.errors.ValidationFailed` with a safe ``field=`` so the
HTTP boundary can render a structured ``validation_failed`` envelope.

Symbols: ``_coerce_annotation_kind``, ``_coerce_viewer_mode``,
``_validate_geometry``, ``_validate_annotation_content``,
``_validate_storage_ref``, ``_validate_lease``.
"""

from __future__ import annotations

from app.domain.errors import ValidationFailed

from .constants import (
    ANNOTATION_KINDS,
    MAX_CHECKOUT_LEASE_SECONDS,
    MAX_GEOMETRY_KEYS,
    MAX_STAMP_LENGTH,
    MAX_TEXT_LENGTH,
    VIEWER_MODES,
    AnnotationKind,
    ViewerMode,
)


def _coerce_annotation_kind(kind: AnnotationKind | str) -> AnnotationKind:
    """Resolve a kind string to :class:`AnnotationKind` or refuse it."""
    if isinstance(kind, AnnotationKind):
        return kind
    try:
        return AnnotationKind(str(kind))
    except ValueError:
        raise ValidationFailed(
            f"unknown annotation kind: {kind!r}; expected one of {sorted(ANNOTATION_KINDS)}",
            field="kind",
        ) from None


def _coerce_viewer_mode(mode: ViewerMode | str) -> ViewerMode:
    """Resolve a mode string to :class:`ViewerMode` or refuse it."""
    if isinstance(mode, ViewerMode):
        return mode
    try:
        return ViewerMode(str(mode))
    except ValueError:
        raise ValidationFailed(
            f"unknown viewer mode: {mode!r}; expected one of {sorted(VIEWER_MODES)}",
            field="mode",
        ) from None


def _validate_geometry(geometry: dict[str, object]) -> None:
    """Validate an annotation geometry mapping (pure)."""
    if not geometry:
        raise ValidationFailed("geometry is required", field="geometry")
    if len(geometry) > MAX_GEOMETRY_KEYS:
        raise ValidationFailed(
            f"geometry must have <= {MAX_GEOMETRY_KEYS} keys",
            field="geometry",
        )
    for key in geometry:
        if not isinstance(key, str) or not key.strip():
            raise ValidationFailed("geometry keys must be non-blank strings", field="geometry")


def _validate_annotation_content(kind: AnnotationKind, content: str | None) -> None:
    """Enforce kind/content pairing for annotations (pure)."""
    # why: a text_box with no text renders as an empty rectangle the
    # reviewer cannot interpret, so refuse it at construction; other
    # kinds treat text as an optional caption.
    if kind is AnnotationKind.TEXT_BOX and (content is None or not content.strip()):
        raise ValidationFailed("text_box annotations require non-empty content", field="content")
    if content is not None and len(content) > MAX_TEXT_LENGTH:
        raise ValidationFailed(f"content must be <= {MAX_TEXT_LENGTH} chars", field="content")
    if kind is AnnotationKind.STAMP and content is not None and len(content) > MAX_STAMP_LENGTH:
        raise ValidationFailed(
            f"stamp content must be <= {MAX_STAMP_LENGTH} chars", field="content"
        )


def _validate_storage_ref(storage_ref: str) -> None:
    """Refuse blank storage refs (pure)."""
    if not storage_ref or not storage_ref.strip():
        raise ValidationFailed("storage_ref is required", field="storage_ref")


def _validate_lease(lease_seconds: int) -> int:
    """Bound a checkout lease and return it, or refuse it (pure)."""
    if lease_seconds <= 0:
        raise ValidationFailed(
            f"lease_seconds must be > 0, got {lease_seconds}",
            field="lease_seconds",
        )
    if lease_seconds > MAX_CHECKOUT_LEASE_SECONDS:
        raise ValidationFailed(
            f"lease_seconds must be <= {MAX_CHECKOUT_LEASE_SECONDS}, got {lease_seconds}",
            field="lease_seconds",
        )
    return lease_seconds


__all__ = [
    "ANNOTATION_KINDS",
    "MAX_CHECKOUT_LEASE_SECONDS",
    "MAX_GEOMETRY_KEYS",
    "MAX_STAMP_LENGTH",
    "MAX_TEXT_LENGTH",
    "VIEWER_MODES",
    "AnnotationKind",
    "ValidationFailed",
    "ViewerMode",
    "_coerce_annotation_kind",
    "_coerce_viewer_mode",
    "_validate_annotation_content",
    "_validate_geometry",
    "_validate_lease",
    "_validate_storage_ref",
]
