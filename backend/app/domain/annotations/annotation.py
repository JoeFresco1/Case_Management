"""``Annotation`` — the viewer overlay aggregate pinned to a document version + page.

An annotation is markup (highlight / text-box / stamp / freehand), never a
content mutation: the viewer renders the overlay layer while the document
bytes stay owned by the ``Document`` aggregate
(:mod:`app.domain.document`). Structural invariants (page/version >= 1,
geometry shape, kind/content pairing) are validated eagerly in
``__post_init__`` so a malformed mark cannot reach a repository (R12
fail-closed, pure domain — no I/O, no vendor SDK per R11).

``# INTEGRATION_SEAM``: persistence goes through the ``AnnotationRepository``
port (``app.domain.ports.viewer``); the local adapter lives under
``app/infrastructure/persistence/``, an Azure SQL adapter plugs in behind
the same Protocol.

Symbols: ``Annotation``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import ValidationFailed

from .constants import AnnotationKind
from .helpers import _coerce_annotation_kind, _validate_annotation_content, _validate_geometry


@dataclass(frozen=True, slots=True)
class Annotation:
    """One viewer annotation pinned to a document version + page.

    An annotation is markup (highlight / text-box / stamp / freehand),
    not a content edit: it carries a ``geometry`` mapping (page-relative
    bounds plus style hints, e.g. ``{"x": 0.1, "y": 0.2, "w": 0.3,
    "h": 0.05, "color": "yellow"}``) and optional text ``content``.
    The viewer renders the overlay; the document bytes are untouched.

    ``document_version`` pins the mark to the version it was drawn
    against so revision history can show "drawn on v2, viewing v3".
    Annotations are append-only from the domain's perspective — edit or
    removal is a repository concern recorded with its own audit event.
    """

    id: UUID
    tenant_id: UUID
    document_id: UUID
    document_version: int
    page: int
    kind: AnnotationKind
    geometry: dict[str, object]
    content: str | None
    created_by: UUID
    created_at: datetime

    def __post_init__(self) -> None:
        """Validate structural invariants (pure, no I/O)."""
        if self.document_version < 1:
            raise ValidationFailed(
                f"document_version must be >= 1, got {self.document_version}",
                field="document_version",
            )
        if self.page < 1:
            raise ValidationFailed(f"page must be >= 1, got {self.page}", field="page")
        _validate_geometry(self.geometry)
        _validate_annotation_content(self.kind, self.content)

    @staticmethod
    def create(
        *,
        tenant_id: UUID,
        document_id: UUID,
        document_version: int,
        page: int,
        kind: AnnotationKind | str,
        geometry: dict[str, object],
        content: str | None,
        author_id: UUID,
        now: datetime,
    ) -> Annotation:
        """Construct a fresh annotation.

        Args:
            tenant_id: Owning tenant (isolation key, never inferred).
            document_id: Document the mark overlays.
            document_version: Version the mark was drawn against (>= 1).
            page: 1-based page number.
            kind: One of ``highlight`` / ``text_box`` / ``stamp`` /
                ``freehand`` (string or :class:`AnnotationKind`).
            geometry: Page-relative bounds + style hints (non-empty,
                <= ``MAX_GEOMETRY_KEYS`` keys, non-blank string keys).
            content: Optional overlay text (required for ``text_box``,
                optional for ``highlight``/``stamp``/``freehand``).
            author_id: User who drew the mark.
            now: Creation instant (caller threads the Clock port).

        Raises:
            ValidationFailed: On bad page/version, unknown kind,
                malformed geometry, or kind/content mismatch.
        """
        resolved = _coerce_annotation_kind(kind)
        # why: copy the caller mapping so later caller-side mutation
        # cannot silently alter the frozen aggregate's overlay bounds.
        snapshot = dict(geometry)
        return Annotation(
            id=uuid4(),
            tenant_id=tenant_id,
            document_id=document_id,
            document_version=document_version,
            page=page,
            kind=resolved,
            geometry=snapshot,
            content=content,
            created_by=author_id,
            created_at=now,
        )

    def to_payload(self) -> dict[str, object]:
        """Render a JSON-safe dict for audit/timeline payloads.

        Geometry values are passed through verbatim (they are viewer
        bounds, never PHI); ``content`` is included only when present.
        """
        out: dict[str, object] = {
            "annotation_id": str(self.id),
            "document_id": str(self.document_id),
            "document_version": self.document_version,
            "page": self.page,
            "kind": self.kind.value,
            "geometry": dict(self.geometry),
            "created_by": str(self.created_by),
            "created_at": self.created_at.isoformat(),
        }
        if self.content is not None:
            out["content"] = self.content
        return out


__all__ = [
    "Annotation",
    "AnnotationKind",
    "UUID",
    "ValidationFailed",
    "dataclass",
    "datetime",
    "uuid4",
]
