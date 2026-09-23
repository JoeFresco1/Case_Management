"""Viewer-pipeline persistence ports (T5 / FM-023 ext).

The browser-native viewer needs durable rows for three concerns that
the core ``DocumentRepository`` does not cover:

- ``AnnotationRepository`` — the annotation overlay layer. Annotations
  are markup pinned to ``(document_id, document_version, page)``; they
  never mutate document bytes, so they live in their own table and
  the use case never touches ``documents`` / ``document_versions`` on
  the annotation path.
- ``CheckoutLockRepository`` — persistent exclusive checkouts. At most
  one *live* row per document; history is the append-only sequence of
  rows (takeover after expiry mints a fresh row, release stamps the
  existing row).
- ``RenditionRepository`` — burned viewer renditions (redacted burns
  plus derived thumbnail/preview descriptors). The bytes themselves
  live behind ``DocumentStore``; the rows carry content addressing
  (sha256 + size + storage_ref) so the revision/rendition history view
  can render "burned from v2" without reading the store.

Tenant isolation is mandatory (AGENTS.md §3 R1, §12): every method
takes ``tenant_id`` first and the SQL adapter MUST include
``WHERE tenant_id = :tenant_id`` on every query. Cross-tenant reads
return ``None`` / empty sequences so a misconfigured caller cannot
leak existence (Constitution V).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.domain.annotations import Annotation, CheckoutLock, Rendition
from app.domain.ports.core import TenantScoped


class AnnotationRepository(TenantScoped, Protocol):
    """Persistence port for :class:`app.domain.annotations.Annotation`.

    The repository is a thin surface: validation lives in the domain
    aggregate (``Annotation.create``), authorization in the use case.
    Update/delete are repository-level operations recorded with their
    own audit events by the caller — the domain aggregate itself stays
    append-only shaped so the overlay history is explainable.
    """

    async def add(self, annotation: Annotation) -> None:
        """Persist a freshly created annotation row.

        Implementations MUST insert the row in the same transaction as
        the caller's audit row so a partial commit never leaves markup
        without provenance.
        """

    async def get(self, tenant_id: UUID, annotation_id: UUID) -> Annotation | None:
        """Return one annotation row, or ``None``.

        Returns ``None`` for missing ids AND for ids belonging to a
        different tenant; the two cases are intentionally
        indistinguishable (Constitution V).
        """

    async def list_for_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        document_version: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Annotation]:
        """Return annotation rows for a document, oldest first.

        ``document_version`` optionally pins the overlay to the version
        it was drawn against ("drawn on v2, viewing v3"). Tenant-scoped;
        a cross-tenant ``document_id`` returns the empty sequence.
        """

    async def save(self, annotation: Annotation) -> None:
        """Persist geometry/content edits on an existing annotation row.

        Tenant-scoped UPDATE: a cross-tenant id cannot match the WHERE
        clause. Only the overlay columns change — the pinned
        ``(document_id, document_version, page, kind)`` identity and the
        ``created_by`` / ``created_at`` provenance are immutable.
        """

    async def delete(self, tenant_id: UUID, annotation_id: UUID) -> bool:
        """Delete an annotation row, returning ``True`` iff removed.

        Returns ``False`` for missing ids AND for ids belonging to a
        different tenant. The caller audits the removal as its own
        event so the timeline stays explainable.
        """


class CheckoutLockRepository(TenantScoped, Protocol):
    """Persistence port for :class:`app.domain.annotations.CheckoutLock`.

    Checkout *history* is the append-only sequence of these rows per
    document. The live-lock rule (at most one unreleased, unexpired row
    per document) is enforced by the use case via
    ``CheckoutLock.check_acquire``; the repository is a thin surface
    that stores rows and lists the trail.
    """

    async def add(self, lock: CheckoutLock) -> None:
        """Persist a freshly acquired checkout row.

        Takeover after expiry mints a fresh row (preserving history);
        the caller has already refused live foreign locks via
        ``check_acquire`` before calling.
        """

    async def get_active(self, tenant_id: UUID, document_id: UUID) -> CheckoutLock | None:
        """Return the latest unreleased checkout row for a document.

        Returns ``None`` when no row exists, when the latest row was
        released, or for a cross-tenant ``document_id``. Liveness
        (expiry) is evaluated by the caller with the ``Clock`` port —
        the repository never reads the clock (AGENTS.md R10).
        """

    async def save(self, lock: CheckoutLock) -> None:
        """Persist a renew/release transition on an existing row.

        Tenant-scoped UPDATE on ``(id, tenant_id)``. Renew rewrites
        ``expires_at``; release stamps ``released_at``.
        """

    async def list_history(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[CheckoutLock]:
        """Return the checkout trail for a document, newest first.

        Tenant-scoped; a cross-tenant ``document_id`` returns the empty
        sequence. Released, expired, and live rows are all included so
        the case timeline can render "who held the document when".
        """


class RenditionRepository(TenantScoped, Protocol):
    """Persistence port for :class:`app.domain.annotations.Rendition`.

    Rendition rows describe viewer-servable bytes (redacted burns;
    thumbnail/preview derivatives). The bytes are written through
    ``DocumentStore.put`` by the caller *before* the row is staged, so
    a row always points at durably stored content. The original
    ``Document`` / ``DocumentVersion`` rows are never modified on this
    path (T5 acceptance: the annotation/rendition layer never mutates
    original bytes).
    """

    async def add(self, rendition: Rendition) -> None:
        """Persist a burned rendition row.

        Implementations MUST insert the row in the same transaction as
        the caller's audit + outbox rows so a burn without provenance
        can never commit.
        """

    async def list_for_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Rendition]:
        """Return rendition rows for a document, oldest first.

        Tenant-scoped; a cross-tenant ``document_id`` returns the empty
        sequence. Used by the revision/rendition history view alongside
        ``DocumentRepository.list_versions``.
        """


__all__ = [
    "AnnotationRepository",
    "CheckoutLockRepository",
    "RenditionRepository",
]
