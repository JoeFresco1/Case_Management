"""Scan-batch inbox + page-workspace rules (T1 capture parity / FM-029 ext).

This module is the **pure-domain** kernel for the two scan paragraphs of
``specs/002-leftovers/spec.md`` §3.T1:

- *"Scan intake with device/source provenance, scan-batch inbox, page
  split/join/merge workspace, hold-until-verified."*
- The acceptance lines *"Scan/batch items carry device/source provenance;
  separation actions preserve page audit trail."*

# What a scan batch is

A scanner (or the local ``scan_stub`` adapter) delivers pages, not cases.
The batch is the inbox row the operator triages: pages accumulate while
the batch is ``OPEN``, the operator (or the separator-page rule in
:mod:`app.domain.extraction`) declares it complete, a second human
verifies it, and only then may documents be filed against a case. That
last gate is the **hold-until-verified** rule: verification is a
human authorization, never an adapter verdict (Constitution XIV).

# Provenance

Every batch and every page carries device/source provenance
(``device_id``, ``source_label``, channel identity). The provenance is
part of the durable row so the audit timeline can render "page 3 came
from scanner S via channel C" without joining any other table. Payload
bytes never live here — only content addressing (``sha256`` + ``size``
+ ``storage_ref``), mirroring :class:`app.domain.document.Document`.

# Page workspace linkage

Page surgery itself lives in :mod:`app.domain.separation` (split / join /
merge with the page audit trail). This module is the feeder: a verified
batch projects its pages into :class:`SeparationSource
<app.domain.separation.SeparationSource>` descriptors via
:meth:`ScanBatch.to_separation_source` so the operator can separate a
scanned batch without re-typing page geometry. The separation record
pins the batch's page versions, which is how the audit proves "split
from this exact scan, not from a later edit".

# INTEGRATION_SEAM: scan device connector

Real scanners arrive via the P14 connector (AGENTS.md §12); the local
adapter accepts JSON-shaped page descriptors over HTTP today. The domain
never touches the device — it validates descriptors the application
layer already hashed + stored through ``DocumentStore``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from app.domain.errors import Forbidden, InvariantViolation, ValidationFailed

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids a runtime cycle
    from app.domain.separation import SeparationSource

# Maximum pages per batch. A batch is an interactive inbox, not a bulk
# import (bulk movement is the T1 package path with its own mapping
# version + reconciliation report). The cap keeps the inbox row and its
# audit payload bounded.
MAX_BATCH_PAGES = 500

_MAX_LABEL_CHARS = 200


class ScanBatchStatus(StrEnum):
    """The inbox lifecycle of a :class:`ScanBatch`.

    - ``OPEN`` — receiving pages; the device may still be feeding.
    - ``READY`` — page intake complete, awaiting human verification.
    - ``VERIFIED`` — a human verified the batch; documents may be filed.
    - ``RELEASED`` — documents were filed from this batch (terminal).
    - ``REJECTED`` — operator dropped the batch (terminal, audited).
    """

    OPEN = "open"
    READY = "ready"
    VERIFIED = "verified"
    RELEASED = "released"
    REJECTED = "rejected"


_TERMINAL_BATCH_STATUSES: frozenset[ScanBatchStatus] = frozenset(
    {ScanBatchStatus.RELEASED, ScanBatchStatus.REJECTED}
)


def is_terminal_batch_status(status: ScanBatchStatus) -> bool:
    """Return True iff ``status`` ends the batch lifecycle."""
    return status in _TERMINAL_BATCH_STATUSES


@dataclass(frozen=True, slots=True)
class ScanPage:
    """One page inside a scan batch (descriptor only, no bytes).

    The bytes were already hashed + stored through ``DocumentStore`` by
    the application layer; this row carries the content address plus
    the per-page device provenance so the audit can attribute each page
    individually (a batch may span feeder trays or even devices when a
    jam forces a rescan on a second scanner).
    """

    page_number: int
    sha256: str
    size: int
    storage_ref: str
    device_id: str
    captured_at: datetime

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise InvariantViolation(f"scan page_number must be >= 1, got {self.page_number}")
        if not self.sha256 or len(self.sha256) != 64:
            raise InvariantViolation("scan page requires a 64-char sha256 digest")
        if self.size < 0:
            raise InvariantViolation(f"scan page size must be >= 0, got {self.size}")
        if not self.storage_ref:
            raise InvariantViolation("scan page requires a storage_ref")
        if not self.device_id:
            raise InvariantViolation("scan page requires a device_id for provenance")


@dataclass(frozen=True, slots=True)
class ScanSeparationSource:
    """Per-page separation descriptor preserving scan provenance.

    The workspace-level :class:`SeparationSource
    <app.domain.separation.SeparationSource>` is document-level (one
    entry per input document with ``document_id`` / ``version_number`` /
    ``page_count``); a scan batch is page-level (one entry per scanned
    page with its own content address + device). This shape is the
    page-level projection the separation workspace consumes when the
    operator separates a scanned batch: one descriptor per page pinning
    the exact bytes (``sha256`` + ``size`` + ``storage_ref``), the
    capturing device (``device_id``), and the batch/channel identity
    (``batch_id`` / ``channel_id`` / ``channel_key`` / ``source_label``).

    Why a dedicated shape instead of reusing ``SeparationSource``
    directly: stuffing per-page hashes into a document-level descriptor
    would silently drop the per-page audit trail (the T1 acceptance
    requires "separation actions preserve page audit trail"). Callers
    that need a document-level plan group these descriptors by
    ``batch_id`` and build a single ``SeparationSource`` with
    ``page_count=len(sources)``; the per-page rows remain the proof of
    "split from this exact scan".

    Device / OCR engines stay behind ``ScanAuthorizationProvider`` +
    ``OcrProvider`` Protocols; this shape carries only descriptors,
    never bytes or vendor handles.
    """

    batch_id: UUID
    tenant_id: UUID
    case_id: UUID | None
    page_number: int
    sha256: str
    size: int
    storage_ref: str
    device_id: str
    channel_id: UUID
    channel_key: str
    source_label: str

    def to_audit_dict(self) -> dict[str, str]:
        """Return a redaction-safe audit mapping for one page.

        Carries identity + content address only — never bytes or file
        names — so the separation audit row can reference the exact
        page without leaking document metadata (Constitution V).
        """
        return {
            "batch_id": str(self.batch_id),
            "page_number": str(self.page_number),
            "sha256": self.sha256,
            "device_id": self.device_id,
            "channel_key": self.channel_key,
            "source_label": self.source_label,
        }


@dataclass(frozen=True, slots=True)
class ScanBatch:
    """The inbox row for one scanning session.

    Carries the batch-level device/source provenance (the scanner that
    fed the batch, the operator-visible source label, the channel it
    arrived through) plus the ordered page descriptors. Tenant-scoped
    end to end: cross-tenant access returns ``None`` at the repository
    layer and mutating transitions refuse on a ``tenant_id`` mismatch
    here (fail-closed, Constitution V).
    """

    id: UUID
    tenant_id: UUID
    channel_id: UUID
    channel_key: str
    device_id: str
    source_label: str
    status: ScanBatchStatus
    pages: tuple[ScanPage, ...]
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    verified_by: UUID | None = None
    verified_at: datetime | None = None
    case_id: UUID | None = None
    context: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.device_id:
            raise InvariantViolation("scan batch requires a device_id for provenance")
        if not self.source_label or len(self.source_label) > _MAX_LABEL_CHARS:
            raise InvariantViolation(
                f"scan batch source_label is required (1..{_MAX_LABEL_CHARS} chars)"
            )
        if not self.channel_key:
            raise InvariantViolation("scan batch requires a channel_key for provenance")
        # Why enforce dense numbering here: a gap would silently drop a
        # page from the audit trail (neither the batch nor a separated
        # document would own it) — the same partition argument as
        # ``separation.validate_partition``.
        for index, page in enumerate(self.pages, start=1):
            if page.page_number != index:
                raise InvariantViolation(
                    "scan batch pages must be densely numbered from 1: "
                    f"position {index} carries page_number {page.page_number}"
                )

    # ----- factories ------------------------------------------------------

    @staticmethod
    def open(
        *,
        tenant_id: UUID,
        channel_id: UUID,
        channel_key: str,
        device_id: str,
        source_label: str,
        created_by: UUID,
        now: datetime,
        case_id: UUID | None = None,
    ) -> ScanBatch:
        """Open a fresh batch inbox for one scanning session."""
        return ScanBatch(
            id=uuid4(),
            tenant_id=tenant_id,
            channel_id=channel_id,
            channel_key=channel_key,
            device_id=device_id,
            source_label=source_label,
            status=ScanBatchStatus.OPEN,
            pages=(),
            created_by=created_by,
            created_at=now,
            updated_at=now,
            case_id=case_id,
        )

    # ----- guards ---------------------------------------------------------

    def _check_tenant(self, tenant_id: UUID) -> None:
        """Refuse cross-tenant mutation before any other validation."""
        # Why first: tenant isolation is deny-by-default (Constitution V,
        # FM-002) on top of ``WHERE tenant_id = :tenant_id``. A caller
        # holding another tenant's batch id must fail closed even when
        # every other field is valid.
        if tenant_id != self.tenant_id:
            raise Forbidden(
                "cross-tenant scan batch access denied",
                details={"reason": "cross_tenant"},
            )

    def _check_open(self) -> None:
        """Refuse page intake unless the batch is still open."""
        if self.status is not ScanBatchStatus.OPEN:
            raise ValidationFailed(
                f"scan batch is {self.status.value}; only open batches accept pages",
                field="status",
            )

    # ----- transitions ----------------------------------------------------

    def add_page(
        self,
        *,
        tenant_id: UUID,
        sha256: str,
        size: int,
        storage_ref: str,
        device_id: str,
        now: datetime,
    ) -> ScanBatch:
        """Append one page descriptor to an open batch.

        Page numbers are assigned sequentially (dense from 1) so the
        batch geometry is always partition-ready for the separation
        workspace. Raises on cross-tenant callers, closed batches, and
        over-cap batches before any persistence happens.
        """
        self._check_tenant(tenant_id)
        self._check_open()
        if len(self.pages) >= MAX_BATCH_PAGES:
            raise ValidationFailed(
                f"scan batch supports at most {MAX_BATCH_PAGES} pages",
                field="pages",
            )
        page = ScanPage(
            page_number=len(self.pages) + 1,
            sha256=sha256,
            size=size,
            storage_ref=storage_ref,
            device_id=device_id or self.device_id,
            captured_at=now,
        )
        return replace(self, pages=self.pages + (page,), updated_at=now)

    def mark_ready(self, *, tenant_id: UUID, now: datetime) -> ScanBatch:
        """Declare page intake complete; the batch awaits verification.

        Requires at least one page — an empty batch is a scanner
        misfeed, not a verifiable unit, and must be rejected instead.
        """
        self._check_tenant(tenant_id)
        self._check_open()
        if not self.pages:
            raise ValidationFailed(
                "scan batch has no pages; reject the empty batch instead",
                field="pages",
            )
        return replace(self, status=ScanBatchStatus.READY, updated_at=now)

    def verify(self, *, tenant_id: UUID, by: UUID, now: datetime) -> ScanBatch:
        """Record human verification of a ready batch.

        This is the hold-until-verified gate: only a ``VERIFIED`` batch
        may file documents (the application use case refuses to build
        ``Document`` rows from any other status). The verifier should
        differ from the creator where staffing allows; the domain does
        not enforce that split — the application policy does — so a
        single-operator deployment can still verify its own scans.
        """
        self._check_tenant(tenant_id)
        if self.status is ScanBatchStatus.VERIFIED:
            return self
        if self.status is not ScanBatchStatus.READY:
            raise ValidationFailed(
                f"scan batch is {self.status.value}; only ready batches can be verified",
                field="status",
            )
        return replace(
            self, status=ScanBatchStatus.VERIFIED, verified_by=by, verified_at=now, updated_at=now
        )

    def mark_released(self, *, tenant_id: UUID, now: datetime) -> ScanBatch:
        """Mark the batch released after its documents were filed.

        Requires ``VERIFIED`` — filing from an unverified batch is the
        exact bypass the hold-until-verified rule exists to prevent, so
        the transition refuses any other status even when the caller
        holds every grant.
        """
        self._check_tenant(tenant_id)
        if self.status is ScanBatchStatus.RELEASED:
            return self
        if self.status is not ScanBatchStatus.VERIFIED:
            raise ValidationFailed(
                "scan batch must be verified before documents may be filed "
                f"(status is {self.status.value})",
                field="status",
            )
        return replace(self, status=ScanBatchStatus.RELEASED, updated_at=now)

    def reject(self, *, tenant_id: UUID, now: datetime) -> ScanBatch:
        """Drop the batch (misfeed, wrong tray, test scan). Terminal.

        Idempotent on already-rejected rows; releasing a rejected batch
        raises — the audit trail is append-only (Constitution V).
        """
        self._check_tenant(tenant_id)
        if self.status is ScanBatchStatus.REJECTED:
            return self
        if self.status is ScanBatchStatus.RELEASED:
            raise InvariantViolation("cannot reject a released scan batch")
        return replace(self, status=ScanBatchStatus.REJECTED, updated_at=now)

    # ----- workspace linkage ----------------------------------------------

    @property
    def page_count(self) -> int:
        """Return the number of pages currently in the batch."""
        return len(self.pages)

    def describe_provenance(self) -> dict[str, str]:
        """Return a safe provenance summary for audit payloads and logs.

        Carries ids, device, channel, and page count only — never file
        names, storage refs, or content — so the audit row cannot leak
        document metadata to readers who hold the timeline but not the
        document read grant.
        """
        return {
            "scan_batch_id": str(self.id),
            "device_id": self.device_id,
            "source_label": self.source_label,
            "channel_key": self.channel_key,
            "channel_id": str(self.channel_id),
            "page_count": str(len(self.pages)),
            "status": self.status.value,
        }

    def to_separation_source(self) -> tuple[ScanSeparationSource, ...]:
        """Project verified batch pages into separation descriptors.

        Returns:
            One :class:`ScanSeparationSource` per page, in page order,
            each pinning ``page_number`` + ``sha256`` + ``size`` +
            ``storage_ref`` + ``device_id`` + ``batch_id`` +
            ``channel_key`` / ``channel_id`` + ``source_label`` so the
            separation workspace can prove "split from this exact scan,
            not from a later edit".

        Raises:
            ValidationFailed: When ``status`` is not ``READY`` or
                ``VERIFIED`` (``field="status"``) — unverified batches
                must not enter separation (hold-until-verified,
                Constitution XIV) — or when the batch carries no pages
                (``field="pages"``).

        Why ``READY`` or ``VERIFIED`` (not only ``VERIFIED``): the
        operator triages separation geometry once intake is complete
        (``READY``) and files documents only after human verification
        (``VERIFIED``); both stages need the provenance-pinned
        descriptors, while the filing gate itself stays on
        :meth:`mark_released`.
        """
        # Why status first: an unverified batch must fail closed even
        # when its pages are otherwise well-formed (Constitution V).
        if self.status not in (ScanBatchStatus.READY, ScanBatchStatus.VERIFIED):
            raise ValidationFailed(
                f"scan batch is {self.status.value}; "
                "only ready or verified batches may enter separation",
                field="status",
            )
        if not self.pages:
            raise ValidationFailed(
                "scan batch has no pages; nothing to separate",
                field="pages",
            )
        return tuple(
            ScanSeparationSource(
                batch_id=self.id,
                tenant_id=self.tenant_id,
                case_id=self.case_id,
                page_number=page.page_number,
                sha256=page.sha256,
                size=page.size,
                storage_ref=page.storage_ref,
                device_id=page.device_id,
                channel_id=self.channel_id,
                channel_key=self.channel_key,
                source_label=self.source_label,
            )
            for page in self.pages
        )

    def as_document_separation_source(self) -> SeparationSource:
        """Project the whole batch as one document-level separation source.

        Args:
            None — reads the batch row only.

        Returns:
            A :class:`SeparationSource
            <app.domain.separation.SeparationSource>` with
            ``document_id=batch.id``, ``version_number=1``,
            ``page_count=len(pages)``, ``locked=False`` for callers that
            plan a batch-level split via
            :func:`app.domain.separation.plan_split`. Per-page proof
            remains in :meth:`to_separation_source`; this is the
            document-level handle the plan API takes.

        Raises:
            ValidationFailed: When the batch is not ``READY`` /
                ``VERIFIED`` (``field="status"``), has no pages
                (``field="pages"``), or has no ``case_id``
                (``field="case_id"``) — separation outputs must belong
                to one case.
        """
        if self.status not in (ScanBatchStatus.READY, ScanBatchStatus.VERIFIED):
            raise ValidationFailed(
                f"scan batch is {self.status.value}; "
                "only ready or verified batches may enter separation",
                field="status",
            )
        if not self.pages:
            raise ValidationFailed(
                "scan batch has no pages; nothing to separate",
                field="pages",
            )
        if self.case_id is None:
            raise ValidationFailed(
                "scan batch has no case; separation outputs must belong to one case",
                field="case_id",
            )
        # Imported locally under TYPE_CHECKING for typing; runtime
        # import here keeps the domain cycle-free (scan_batch <->
        # separation) while still returning the real aggregate.
        from app.domain.separation import SeparationSource as _SeparationSource  # noqa: PLC0415

        return _SeparationSource(
            document_id=self.id,
            tenant_id=self.tenant_id,
            case_id=self.case_id,
            version_number=1,
            page_count=len(self.pages),
            locked=False,
        )


__all__ = [
    "MAX_BATCH_PAGES",
    "ScanBatch",
    "ScanBatchStatus",
    "ScanPage",
    "ScanSeparationSource",
    "is_terminal_batch_status",
]
