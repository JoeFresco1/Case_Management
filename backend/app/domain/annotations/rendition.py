"""``Rendition`` descriptor, the read-side viewer resolver, and ``PermissionDenied``.

A rendition points at the ``DocumentVersion`` it was derived from and
carries content addressing (sha256 + size + storage_ref) so identical
burns share storage. :func:`resolve_viewer_rendition` is the pure
read-side guard that decides which rendition kind a viewer mode may
serve: least-privilege (redacted bytes whenever visible), fail-closed
(neither right -> refusal, no existence oracle), originals gated behind
the stricter right (T5 acceptance: "original retrievable under stricter
right; both audited"). :class:`PermissionDenied` is the refusal signal;
the application layer maps it to the ``forbidden`` HTTP envelope.

The domain never touches bytes or a storage SDK (R11) — this module only
describes renditions and picks which one an authorized reader may get.

``# INTEGRATION_SEAM``: serving bytes goes through the ``DocumentStore``
port (``app.domain.ports``); rendition ``storage_ref`` values are opaque
strings minted by the local-FS or Azure-Blob adapter.

Symbols: ``Rendition``, ``resolve_viewer_rendition``, ``PermissionDenied``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.errors import InvariantViolation, ValidationFailed

from .constants import RenditionKind, ViewerMode
from .helpers import _coerce_viewer_mode, _validate_storage_ref


@dataclass(frozen=True, slots=True)
class Rendition:
    """One viewer-servable rendition of a document version.

    A rendition points at ``source_version`` (the ``DocumentVersion``
    it was derived from) and carries content addressing (sha256 + size
    + storage_ref) so identical burns share storage. ``redaction_id``
    is set only for ``REDACTED`` renditions, linking the burned bytes
    back to the mark that authorized them for the revision-history
    view. ``ORIGINAL`` renditions never carry a ``redaction_id``.
    """

    id: UUID
    tenant_id: UUID
    document_id: UUID
    source_version: int
    kind: RenditionKind
    sha256: str
    size: int
    storage_ref: str
    content_type: str
    redaction_id: UUID | None
    created_at: datetime

    def __post_init__(self) -> None:
        """Validate rendition invariants (pure, no I/O)."""
        if self.source_version < 1:
            raise ValidationFailed(
                f"source_version must be >= 1, got {self.source_version}",
                field="source_version",
            )
        if self.size < 0:
            raise ValidationFailed(f"size must be >= 0, got {self.size}", field="size")
        if not self.content_type:
            raise ValidationFailed("content_type is required", field="content_type")
        _validate_storage_ref(self.storage_ref)
        # why: an ORIGINAL rendition IS the filed bytes — linking it to
        # a redaction mark would rewrite history; a REDACTED rendition
        # without a mark would be unauditable, so both are refused.
        if self.kind is RenditionKind.ORIGINAL and self.redaction_id is not None:
            raise ValidationFailed(
                "original renditions must not carry a redaction_id",
                field="redaction_id",
            )
        if self.kind is RenditionKind.REDACTED and self.redaction_id is None:
            raise ValidationFailed(
                "redacted renditions require the authorizing redaction_id",
                field="redaction_id",
            )


def resolve_viewer_rendition(
    *,
    mode: ViewerMode | str,
    has_redacted_rendition: bool,
    may_view_original: bool,
    may_view_redacted: bool,
) -> RenditionKind:
    """Decide which rendition kind a viewer mode may serve (pure rule).

    Read-side guard for the viewer pipeline (T5 acceptance: "original
    retrievable under stricter right; both audited" and "guessed-URL
    download of restricted doc returns ``forbidden``"):

    - The redacted rendition is preferred whenever the caller may see
      it and one exists — the viewer never shows more than authorized.
    - The original requires the stricter ``may_view_original`` right;
      without it the caller gets the redacted rendition (if allowed)
      or a refusal.
    - ``mode`` selects derived renditions only as a preference hint:
      ``thumbnail`` prefers ``THUMBNAIL`` and ``detail`` prefers
      ``PREVIEW`` when the caller may view the (possibly redacted)
      content; ``paged`` serves the full ``ORIGINAL``/``REDACTED``.

    Args:
        mode: ``paged`` | ``thumbnail`` | ``detail``.
        has_redacted_rendition: Whether a burned redacted rendition
            exists for the requested version.
        may_view_original: Caller holds the stricter original-view right.
        may_view_redacted: Caller holds the (weaker) redacted-view right.

    Raises:
        ValidationFailed: On an unknown viewer mode.
        PermissionDenied: When the caller may view neither rendition.

    Returns:
        The :class:`RenditionKind` the adapter should serve.
    """
    resolved_mode = _coerce_viewer_mode(mode)
    # why: fail-closed — a caller with neither right learns nothing
    # about which renditions exist, so refuse before branching on
    # ``has_redacted_rendition`` (no existence oracle).
    if not may_view_original and not may_view_redacted:
        raise PermissionDenied("caller may not view this document in any rendition")
    redacted_visible = has_redacted_rendition and may_view_redacted
    # why: least-privilege default — serve the redacted bytes whenever
    # they exist and the caller may see them, even if the caller could
    # also see the original (the viewer must opt into originals).
    full_kind = RenditionKind.REDACTED if redacted_visible else RenditionKind.ORIGINAL
    if full_kind is RenditionKind.ORIGINAL and not may_view_original:
        # why: reachable only when the caller may see redacted content
        # but no burned rendition exists yet — fall back to the redacted
        # slot so the adapter renders "pending burn" instead of leaking
        # the original.
        full_kind = RenditionKind.REDACTED
    # why: derived renditions are only servable when authorized bytes
    # exist to derive them from; otherwise the caller gets the redacted
    # slot ("pending burn") rather than a thumbnail of nothing (or worse,
    # of bytes they may not see).
    has_derivable_bytes = redacted_visible or may_view_original
    if resolved_mode is ViewerMode.THUMBNAIL and has_derivable_bytes:
        return RenditionKind.THUMBNAIL
    if resolved_mode is ViewerMode.DETAIL and has_derivable_bytes:
        return RenditionKind.PREVIEW
    if resolved_mode is not ViewerMode.PAGED:
        return RenditionKind.REDACTED
    return full_kind


class PermissionDenied(InvariantViolation):
    """Read-side refusal for an unauthorized rendition request.

    A dedicated subclass (rather than ``Forbidden``) keeps this pure
    module free of the authorization-policy vocabulary: the application
    layer maps it to the ``forbidden`` HTTP envelope with no metadata
    leak. Carries no identifiers beyond the message on purpose.
    """


__all__ = [
    "InvariantViolation",
    "PermissionDenied",
    "Rendition",
    "RenditionKind",
    "UUID",
    "ValidationFailed",
    "ViewerMode",
    "dataclass",
    "datetime",
    "resolve_viewer_rendition",
]
