"""``Redaction`` — viewer-proposed redaction mark burned as a new rendition.

The mark records *where* (page + region) and *why* (reason, no PII —
reproduced in audit payloads). Burning an ``APPLIED`` mark produces a
:class:`~.rendition.Rendition` of kind ``REDACTED``; the original
``Document`` / ``DocumentVersion`` rows are never modified, so the
original stays retrievable under a stricter right and both sides are
audited (T5 / FM-025 acceptance). The aggregate is append-only:
``propose`` -> ``apply`` (or ``supersede``); there is no UPDATE/DELETE
path, and bytes are written by the application layer through the
``DocumentStore`` port (R11 — this module only shapes the mark and the
rendition descriptor).

``# INTEGRATION_SEAM``: burning bytes goes through ``DocumentStore.put``
(local FS today, ``AzureBlobDocumentStore`` later); the audit event
``RedactionBurned`` is written in the same transaction by the use case.

Symbols: ``Redaction``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4

from app.domain.errors import InvariantViolation, ValidationFailed

from .constants import MAX_TEXT_LENGTH, RedactionStatus, RenditionKind
from .helpers import _validate_storage_ref
from .rendition import Rendition


@dataclass(frozen=True, slots=True)
class Redaction:
    """A viewer redaction mark on a document version (T5 / FM-025).

    The mark records *where* (page + region description) and *why*
    (reason, 1..500 chars, no PII — reproduced in audit payloads).
    Burning an ``APPLIED`` mark produces a :class:`Rendition` of kind
    ``REDACTED``; the original ``Document`` / ``DocumentVersion`` rows
    are never modified, so the original stays retrievable under a
    stricter right and both sides are audited.

    Status flow: ``PROPOSED`` -> ``APPLIED`` (:meth:`apply`) or a later
    mark ``SUPERSEDE``s an earlier one (:meth:`supersede`). There is no
    UPDATE/DELETE path on the aggregate.
    """

    id: UUID
    tenant_id: UUID
    document_id: UUID
    base_version: int
    page: int
    region: str
    reason: str
    status: RedactionStatus
    created_by: UUID
    created_at: datetime
    applied_at: datetime | None = None
    superseded_by: UUID | None = None

    def __post_init__(self) -> None:
        """Validate structural invariants (pure, no I/O)."""
        if self.base_version < 1:
            raise ValidationFailed(
                f"base_version must be >= 1, got {self.base_version}",
                field="base_version",
            )
        if self.page < 1:
            raise ValidationFailed(f"page must be >= 1, got {self.page}", field="page")
        if not self.region or not self.region.strip():
            raise ValidationFailed("region is required", field="region")
        if len(self.region) > MAX_TEXT_LENGTH:
            raise ValidationFailed(f"region must be <= {MAX_TEXT_LENGTH} chars", field="region")
        if not self.reason or not self.reason.strip():
            raise ValidationFailed("reason is required", field="reason")
        if len(self.reason) > MAX_TEXT_LENGTH:
            raise ValidationFailed(f"reason must be <= {MAX_TEXT_LENGTH} chars", field="reason")

    @staticmethod
    def propose(
        *,
        tenant_id: UUID,
        document_id: UUID,
        base_version: int,
        page: int,
        region: str,
        reason: str,
        author_id: UUID,
        now: datetime,
    ) -> Redaction:
        """Propose a fresh redaction mark in ``PROPOSED`` status.

        The application layer persists the mark and routes it for
        approval; burning happens only via :meth:`apply`.
        """
        return Redaction(
            id=uuid4(),
            tenant_id=tenant_id,
            document_id=document_id,
            base_version=base_version,
            page=page,
            region=region.strip(),
            reason=reason.strip(),
            status=RedactionStatus.PROPOSED,
            created_by=author_id,
            created_at=now,
        )

    def apply(self, *, now: datetime) -> Redaction:
        """Mark this proposal ``APPLIED`` (approved for burn).

        Only a ``PROPOSED`` mark can be applied; applying records the
        burn instant. The caller (use case) mints the ``REDACTED``
        rendition bytes via ``DocumentStore.put`` in the same
        transaction and writes the ``RedactionBurned`` audit event.
        """
        if self.status is not RedactionStatus.PROPOSED:
            raise InvariantViolation(
                f"redaction {self.id} is {self.status.value}; only proposed marks can be applied"
            )
        return replace(self, status=RedactionStatus.APPLIED, applied_at=now)

    def supersede(self, *, superseding_id: UUID) -> Redaction:
        """Mark this redaction superseded by a newer mark.

        Supersession preserves history: the earlier mark stays on the
        revision trail with a pointer to its replacement instead of
        being deleted.
        """
        if self.status is RedactionStatus.SUPERSEDED:
            raise InvariantViolation(f"redaction {self.id} is already superseded")
        return replace(
            self,
            status=RedactionStatus.SUPERSEDED,
            superseded_by=superseding_id,
        )

    def burn_as_rendition(
        self,
        *,
        sha256: str,
        size: int,
        storage_ref: str,
        content_type: str,
        now: datetime,
    ) -> Rendition:
        """Describe the ``REDACTED`` rendition burned from this mark.

        Why a descriptor, not bytes: the domain never touches a storage
        SDK (R11). The application layer stores the redacted bytes
        first, then calls this pure constructor to shape the rendition
        row it persists. Only an ``APPLIED`` mark can burn — burning a
        mere proposal is a programmer error.
        """
        if self.status is not RedactionStatus.APPLIED:
            raise InvariantViolation(
                f"redaction {self.id} is {self.status.value}; only applied marks burn as renditions"
            )
        _validate_storage_ref(storage_ref)
        if size < 0:
            raise ValidationFailed(f"size must be >= 0, got {size}", field="size")
        if not content_type:
            raise ValidationFailed("content_type is required", field="content_type")
        return Rendition(
            id=uuid4(),
            tenant_id=self.tenant_id,
            document_id=self.document_id,
            source_version=self.base_version,
            kind=RenditionKind.REDACTED,
            sha256=sha256,
            size=size,
            storage_ref=storage_ref,
            content_type=content_type,
            redaction_id=self.id,
            created_at=now,
        )


__all__ = [
    "InvariantViolation",
    "MAX_TEXT_LENGTH",
    "Redaction",
    "RedactionStatus",
    "Rendition",
    "RenditionKind",
    "UUID",
    "ValidationFailed",
    "dataclass",
    "datetime",
    "replace",
    "uuid4",
]
