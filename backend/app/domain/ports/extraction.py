"""Extraction-adapter + scan-batch ports (T1 assisted indexing / FM-029 ext).

The domain owns *decisions* about extraction outputs
(:mod:`app.domain.extraction`); the adapters own *production* of those
outputs from pixels. This module is the boundary between the two. No
vendor SDK may appear in ``app/domain`` — engines (barcode libraries,
OCR/ICR engines, form recognizers) are only ever referenced through
these Protocols and implemented in ``app/infrastructure/`` (acceptance:
*"no vendor SDK in domain"*).

# INTEGRATION_SEAM: extraction engines

| Port | Local adapter (today) | Production adapter (later) |
|---|---|---|
| ``BarcodeDecoder`` | test fake / stub | barcode engine sidecar |
| ``OcrProvider`` | test fake / stub | Azure AI Vision Read |
| ``TemplateZoneExtractor`` | test fake / stub | Azure Form Recognizer custom template |
| ``HandwritingRecognizer`` | test fake (ICR optional) | ICR engine sidecar |

Each adapter is wired via ``app/config.py`` + ``app/deps.py``; the
application use case depends on the Protocol only. Confidence scores
are adapter-reported; the *threshold* (``MIN_TRUSTED_CONFIDENCE``) is a
domain constant so operator expectations do not drift per vendor.

# Scan-batch persistence

``ScanBatchRepository`` is the durable inbox surface for
:mod:`app.domain.scan_batch`. Tenant isolation is mandatory (AGENTS.md
§3 R1, §12): every method takes ``tenant_id`` first and the SQL adapter
MUST include ``WHERE tenant_id = :tenant_id`` on every query.
Cross-tenant reads return ``None`` / empty sequences so a misconfigured
caller cannot leak existence (Constitution V).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.domain.extraction import (
    Barcode,
    OcrPageResult,
    TemplateDefinition,
    ZoneResult,
)
from app.domain.ports.core import TenantScoped
from app.domain.scan_batch import ScanBatch


class BarcodeDecoder(Protocol):
    """Decode barcode values from one stored page (adapter seam).

    The adapter reads the bytes behind ``storage_ref`` (fetched through
    ``DocumentStore`` by the caller) and reports the decoded values.
    It never writes domain rows; the application use case feeds the
    results into :func:`app.domain.extraction.assign_from_barcode` and
    :func:`app.domain.extraction.split_on_separator_pages`.
    """

    async def decode_page(
        self,
        tenant_id: UUID,
        storage_ref: str,
        page_number: int,
    ) -> Sequence[Barcode]:
        """Return the barcodes found on one page, in scan order.

        Returns the empty sequence when the page carries no barcode —
        that is the common case, not an error.
        """


class OcrProvider(Protocol):
    """Full-page + zonal + point-and-shoot OCR text extraction seam.

    One port covers all three OCR shapes because they differ only in
    *which regions* are read, not in how the engine is invoked: the
    full page is the degenerate "whole-page zone", and point-and-shoot
    is the degenerate single-zone request. A second code path would be
    the clever twin Constitution XV tells us to delete.
    """

    async def read_full_page(
        self,
        tenant_id: UUID,
        storage_ref: str,
        page_number: int,
    ) -> OcrPageResult:
        """Extract the full-page text of one stored page."""

    async def read_zones(
        self,
        tenant_id: UUID,
        storage_ref: str,
        page_number: int,
        zone_keys: Sequence[str],
    ) -> Sequence[ZoneResult]:
        """Extract text for the named zones of one stored page.

        ``zone_keys`` name adapter-configured regions (template zones
        or operator-drawn rectangles resolved by the caller). Unknown
        keys produce no entry — never an exception — so a stale zone
        map degrades to partial extraction the verifier can see.
        """


class TemplateZoneExtractor(Protocol):
    """Template-zone extraction seam (form ID zone + data zones).

    Resolves which published :class:`TemplateDefinition` a page matches
    (via its form-ID zone) and extracts the data zones. The *decision*
    about the match lives in
    :func:`app.domain.extraction.apply_template`; the adapter only
    produces the zone readings.
    """

    async def extract(
        self,
        tenant_id: UUID,
        storage_ref: str,
        page_number: int,
        template: TemplateDefinition,
    ) -> Sequence[ZoneResult]:
        """Extract all zones of ``template`` (form ID + data) from one page."""


class HandwritingRecognizer(Protocol):
    """Handwriting-recognition (ICR) seam — optional adapter.

    ICR is explicitly optional: deployments without the engine simply
    do not wire this port, and handwritten zones fall back to manual
    indexing (``needs_review=True``). When wired, results are consumed
    exactly like OCR zone results with ``source="icr"`` so the audit
    shows which engine proposed each value.
    """

    async def read_handwriting(
        self,
        tenant_id: UUID,
        storage_ref: str,
        page_number: int,
        zone_keys: Sequence[str],
    ) -> Sequence[ZoneResult]:
        """Extract handwritten text for the named zones of one page."""


class ScanBatchRepository(TenantScoped, Protocol):
    """Persistence port for :class:`app.domain.scan_batch.ScanBatch`.

    The repository is a thin surface: lifecycle validation lives in the
    aggregate (``ScanBatch.add_page / mark_ready / verify / ...``),
    authorization in the use case. Page descriptors are stored as part
    of the batch row (JSON column, portable ``JSON`` type — never
    ``JSONB``) so the inbox read is a single tenant-scoped query.
    """

    async def add(self, batch: ScanBatch) -> None:
        """Persist a freshly opened batch row.

        Implementations MUST insert the row in the same transaction as
        the caller's audit row so a batch without provenance can never
        commit.
        """

    async def get(self, tenant_id: UUID, batch_id: UUID) -> ScanBatch | None:
        """Return one batch row, or ``None``.

        Returns ``None`` for missing ids AND for ids belonging to a
        different tenant; the two cases are intentionally
        indistinguishable (Constitution V).
        """

    async def list_for_channel(
        self,
        tenant_id: UUID,
        channel_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ScanBatch]:
        """Return batch rows for a channel, newest first.

        Tenant-scoped; a cross-tenant ``channel_id`` returns the empty
        sequence. This is the scan-batch inbox query.
        """

    async def save(self, batch: ScanBatch) -> None:
        """Persist page/lifecycle transitions on an existing batch row.

        Tenant-scoped UPDATE on ``(id, tenant_id)``. Only page
        descriptors and lifecycle columns change — the batch provenance
        (``device_id``, ``source_label``, ``channel_id``,
        ``created_by``) is immutable after opening.
        """


__all__ = [
    "BarcodeDecoder",
    "HandwritingRecognizer",
    "OcrProvider",
    "ScanBatchRepository",
    "TemplateZoneExtractor",
]
