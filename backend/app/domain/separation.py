"""Separation workspace rules: split / join / merge preserving the page audit trail (T5).

This module is the **pure-domain** kernel for the T5 document-operations
rule from ``specs/002-leftovers/spec.md`` §3.T5 ("separation workspace
(split/join/merge preserving audit)") and the T1 acceptance line
("separation actions preserve page audit trail"):

- **Split** — one source document is partitioned into N new documents.
  The page ranges must *fully partition* the source (every page ``1..N``
  covered exactly once, no overlaps, no gaps) so the audit can prove
  "no page lost, no page duplicated".
- **Join / Merge** — N source documents concatenate (in the given order)
  into one new document. All sources must belong to the same tenant and
  the same case; cross-case combination is rejected here because
  provenance across cases belongs to the case-relationship domain
  (``app.domain.case_relationship``), not to page surgery.
- **Audit preservation** — every plan mints a ``SeparationRecord`` that
  carries the source document ids, the source *versions* (so the audit
  can prove "split from v3, not from a later edit"), the output ids,
  the page mapping, the actor, and the reason. The sources themselves
  are never mutated: the application layer constructs the new
  ``Document`` aggregates and persists the record in the same
  transaction as the audit hash + outbox row (FR-L02).

# Why a plan, not the documents themselves

The domain does not know how bytes are stored (``DocumentStore`` seam)
and must not mint ``Document`` metadata rows on its own — storage refs
come from the application layer after the bytes land. The plan
therefore validates everything knowable up front (tenant, lock state,
page geometry, reason) and mints the *output ids* so the audit row can
reference documents that the application constructs immediately after.
An invalid plan raises before any byte is stored or any row is written.

# INTEGRATION_SEAM: scan-batch provenance

Scan intake (T1) feeds this workspace: a scanned batch arrives as one
document with ``page_count`` pages plus device/source provenance, and
the operator separates it here. The ``SeparationSource`` descriptor is
the boundary shape the scan use case fills in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from app.domain.errors import DocumentLocked, Forbidden, ValidationFailed

# Maximum page ranges in a single split. A batch with more pages than
# this is a bulk-import concern (T1 packages), not an interactive
# separation; the cap keeps the partition check linear and the audit
# payload bounded.
MAX_SPLIT_OUTPUTS = 200

# Maximum sources in a single join/merge. Same rationale: interactive
# surgery, not bulk reassembly.
MAX_MERGE_SOURCES = 200

_MAX_REASON_CHARS = 500


class SeparationOperation(StrEnum):
    """The kind of page surgery a ``SeparationRecord`` describes."""

    # One source document partitioned into N outputs.
    SPLIT = "split"
    # N sources from one case concatenated in order into one output.
    # Join preserves source order; it is the ordered twin of merge.
    JOIN = "join"
    # N sources combined into one output (order given by the caller).
    MERGE = "merge"


@dataclass(frozen=True, slots=True)
class PageRange:
    """One 1-based inclusive page interval, e.g. pages 3..7 of a batch."""

    start_page: int
    end_page: int

    def __post_init__(self) -> None:
        if self.start_page < 1:
            raise ValueError(f"start_page must be >= 1, got {self.start_page}")
        if self.end_page < self.start_page:
            raise ValueError(
                f"end_page ({self.end_page}) must be >= start_page ({self.start_page})"
            )

    @property
    def page_count(self) -> int:
        """Return the number of pages covered by this range."""
        return self.end_page - self.start_page + 1


@dataclass(frozen=True, slots=True)
class SeparationSource:
    """The boundary shape the caller fills in for each input document.

    The domain never fetches documents itself (no I/O); the application
    use case loads the aggregates and projects them into this shape so
    the plan stays pure and unit-testable.
    """

    document_id: UUID
    tenant_id: UUID
    case_id: UUID
    version_number: int
    page_count: int
    locked: bool


@dataclass(frozen=True, slots=True)
class SeparationRecord:
    """The durable audit-trail row for one split / join / merge.

    The record is append-only: sources are preserved (never mutated by
    the separation itself) and the outputs are new documents. A reader
    can reconstruct "output X came from pages a..b of source S at
    version V, separated by actor A for reason R" without joining any
    other table — that is the page audit trail the T5 acceptance
    requires.
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    operation: SeparationOperation
    source_document_ids: tuple[UUID, ...]
    source_versions: tuple[int, ...]
    output_document_ids: tuple[UUID, ...]
    # For SPLIT: ``page_ranges[i]`` produced ``output_document_ids[i]``.
    # For JOIN/MERGE: empty — order is the source order.
    page_ranges: tuple[PageRange, ...]
    actor_id: UUID
    reason: str
    occurred_at: datetime

    def describe(self) -> dict[str, object]:
        """Return a safe summary for audit payloads and logs.

        Carries ids, versions, and page geometry only — never file
        names, storage refs, or content — so the audit row cannot leak
        document metadata to readers who hold the timeline but not the
        document read grant.
        """
        return {
            "separation_id": str(self.id),
            "operation": self.operation.value,
            "sources": [str(s) for s in self.source_document_ids],
            "source_versions": list(self.source_versions),
            "outputs": [str(o) for o in self.output_document_ids],
            "page_ranges": [
                {"start_page": r.start_page, "end_page": r.end_page} for r in self.page_ranges
            ],
        }


@dataclass(frozen=True, slots=True)
class SeparationPlan:
    """The validated result of ``plan_split`` / ``plan_join_merge``.

    The application use case persists ``record`` and constructs one new
    ``Document`` per id in ``record.output_document_ids`` (fetching
    bytes via ``DocumentStore``) in the same transaction as the audit
    hash + outbox row.
    """

    record: SeparationRecord
    # Parallel to ``record.output_document_ids`` for SPLIT (the page
    # slice each output must be built from); empty for JOIN/MERGE.
    output_ranges: tuple[PageRange, ...] = field(default=())


def _check_tenant(*, plan_tenant_id: UUID, source: SeparationSource) -> None:
    """Deny cross-tenant separation before any other validation."""
    # Why first: tenant isolation is deny-by-default (Constitution V,
    # FM-002) on top of ``WHERE tenant_id = :tenant_id``. A source from
    # another tenant must fail closed even when every other field is
    # valid, and the details carry only a safe reason code.
    if source.tenant_id != plan_tenant_id:
        raise Forbidden(
            "cross-tenant separation denied",
            details={"reason": "cross_tenant"},
        )


def _check_unlocked(source: SeparationSource) -> None:
    """Refuse filed (locked) sources before any side effect."""
    # Why before geometry checks: a locked document refuses *any*
    # separation, so callers get one deterministic refusal instead of
    # a validation error that masks the filing lock (mirrors
    # ``document_protection.validate_redaction`` ordering).
    if source.locked:
        raise DocumentLocked(f"document {source.document_id} is filed; separation is refused")


def _check_reason(reason: str) -> str:
    """Validate the operator's justification and return the stripped form."""
    stripped = reason.strip() if isinstance(reason, str) else ""
    if not stripped or len(stripped) > _MAX_REASON_CHARS:
        raise ValidationFailed(
            "separation reason is required (1..500 chars)",
            field="reason",
        )
    return stripped


def validate_partition(*, page_count: int, ranges: tuple[PageRange, ...]) -> None:
    """Validate that ``ranges`` fully partition pages ``1..page_count``.

    Args:
        page_count: The source document's page count (must be >= 1).
        ranges: The candidate split geometry, in any order.

    Raises:
        ValidationFailed: With ``field`` set to ``page_count`` when the
            source has no pages, or ``page_ranges`` when the ranges are
            empty, out of bounds, overlapping, gapped, or exceed
            ``MAX_SPLIT_OUTPUTS``.
    """
    if page_count < 1:
        raise ValidationFailed(
            f"page_count must be >= 1, got {page_count}",
            field="page_count",
        )
    if not ranges:
        raise ValidationFailed(
            "at least one page range is required for a split",
            field="page_ranges",
        )
    if len(ranges) > MAX_SPLIT_OUTPUTS:
        raise ValidationFailed(
            f"split supports at most {MAX_SPLIT_OUTPUTS} outputs, got {len(ranges)}",
            field="page_ranges",
        )
    for page_range in ranges:
        if page_range.end_page > page_count:
            raise ValidationFailed(
                f"page range {page_range.start_page}..{page_range.end_page} "
                f"exceeds the source page count ({page_count})",
                field="page_ranges",
            )
    ordered = sorted(ranges, key=lambda r: (r.start_page, r.end_page))
    # Why full coverage, not just non-overlap: a gap would silently
    # drop pages from the audit trail (neither source nor output would
    # own them). Forcing an exact partition means "no page lost, no
    # page duplicated" is provable from the record alone.
    cursor = 1
    for page_range in ordered:
        if page_range.start_page != cursor:
            raise ValidationFailed(
                "page ranges must partition the source without gaps or overlaps: "
                f"expected a range starting at page {cursor}, "
                f"got {page_range.start_page}..{page_range.end_page}",
                field="page_ranges",
            )
        cursor = page_range.end_page + 1
    if cursor != page_count + 1:
        raise ValidationFailed(
            f"page ranges cover 1..{cursor - 1} but the source has {page_count} pages",
            field="page_ranges",
        )


def plan_split(
    *,
    tenant_id: UUID,
    case_id: UUID,
    source: SeparationSource,
    ranges: tuple[PageRange, ...],
    actor_id: UUID,
    reason: str,
    now: datetime,
) -> SeparationPlan:
    """Validate a split and mint the plan (output ids + audit record).

    Args:
        tenant_id: The tenant the actor is acting in; must own ``source``.
        case_id: The case the outputs will belong to (same as source's).
        source: The input document descriptor (exactly one).
        ranges: The desired partition geometry (any order; normalized
            into ascending order in the record).
        actor_id: The operator performing the separation.
        reason: Operator justification, 1..500 chars after stripping.
        now: The application clock timestamp (UTC).

    Returns:
        A ``SeparationPlan`` whose record carries one output id per
        range, in ascending page order.

    Raises:
        Forbidden: On cross-tenant source (checked first, fail-closed).
        DocumentLocked: When the source is filed.
        ValidationFailed: On source-case mismatch, bad reason, bad
            page count, or a non-partitioning geometry.
    """
    _check_tenant(plan_tenant_id=tenant_id, source=source)
    _check_unlocked(source)
    if source.case_id != case_id:
        raise ValidationFailed(
            "split outputs must belong to the source's case",
            field="case_id",
        )
    clean_reason = _check_reason(reason)
    validate_partition(page_count=source.page_count, ranges=ranges)
    ordered = tuple(sorted(ranges, key=lambda r: (r.start_page, r.end_page)))
    output_ids = tuple(uuid4() for _ in ordered)
    record = SeparationRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id,
        operation=SeparationOperation.SPLIT,
        source_document_ids=(source.document_id,),
        source_versions=(source.version_number,),
        output_document_ids=output_ids,
        page_ranges=ordered,
        actor_id=actor_id,
        reason=clean_reason,
        occurred_at=now,
    )
    return SeparationPlan(record=record, output_ranges=ordered)


def plan_join_merge(
    *,
    operation: SeparationOperation,
    tenant_id: UUID,
    case_id: UUID,
    sources: tuple[SeparationSource, ...],
    actor_id: UUID,
    reason: str,
    now: datetime,
) -> SeparationPlan:
    """Validate a join/merge and mint the plan (output id + audit record).

    Args:
        operation: ``JOIN`` (ordered concatenation) or ``MERGE``.
        tenant_id: The tenant the actor is acting in; must own every source.
        case_id: The case every source must belong to.
        sources: The input descriptors in output order (2..MAX).
        actor_id: The operator performing the operation.
        reason: Operator justification, 1..500 chars after stripping.
        now: The application clock timestamp (UTC).

    Returns:
        A ``SeparationPlan`` with exactly one output id. The new
        document's page count is the sum of the source page counts
        (exposed via :func:`merged_page_count`).

    Raises:
        Forbidden: On any cross-tenant source (checked first).
        DocumentLocked: When any source is filed.
        ValidationFailed: When ``operation`` is not join/merge, fewer
            than 2 sources are given, a source id repeats, page counts
            are invalid, or any source belongs to another case.
    """
    if operation not in (SeparationOperation.JOIN, SeparationOperation.MERGE):
        raise ValidationFailed(
            f"plan_join_merge supports join/merge, got {operation.value}",
            field="operation",
        )
    # Why tenant before count/shape: fail-closed isolation must hold
    # even for a degenerate (e.g. single-source) request.
    for source in sources:
        _check_tenant(plan_tenant_id=tenant_id, source=source)
    for source in sources:
        _check_unlocked(source)
    if len(sources) < 2:
        raise ValidationFailed(
            "join/merge requires at least 2 source documents "
            "(a single-source copy would fork history silently)",
            field="sources",
        )
    if len(sources) > MAX_MERGE_SOURCES:
        raise ValidationFailed(
            f"join/merge supports at most {MAX_MERGE_SOURCES} sources, got {len(sources)}",
            field="sources",
        )
    seen: set[UUID] = set()
    for source in sources:
        if source.document_id in seen:
            raise ValidationFailed(
                f"source document {source.document_id} is listed twice",
                field="sources",
            )
        seen.add(source.document_id)
        if source.page_count < 1:
            raise ValidationFailed(
                f"source document {source.document_id} has no pages",
                field="sources",
            )
        # Why same-case: cross-case combination changes which case owns
        # the pages, which is a relationship decision with its own
        # provenance + bidirectional navigation
        # (``app.domain.case_relationship``), not page surgery.
        if source.case_id != case_id:
            raise ValidationFailed(
                f"source document {source.document_id} belongs to another case",
                field="sources",
            )
    clean_reason = _check_reason(reason)
    record = SeparationRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id,
        operation=operation,
        source_document_ids=tuple(s.document_id for s in sources),
        source_versions=tuple(s.version_number for s in sources),
        output_document_ids=(uuid4(),),
        page_ranges=(),
        actor_id=actor_id,
        reason=clean_reason,
        occurred_at=now,
    )
    return SeparationPlan(record=record)


def merged_page_count(sources: tuple[SeparationSource, ...]) -> int:
    """Return the output page count for a validated join/merge source list.

    Pure arithmetic over the source descriptors: concatenation sums
    pages. The application use case stores this on the new ``Document``
    metadata so viewers can render the merged page count without
    re-reading the record.
    """
    return sum(source.page_count for source in sources)


__all__ = [
    "MAX_MERGE_SOURCES",
    "MAX_SPLIT_OUTPUTS",
    "PageRange",
    "SeparationOperation",
    "SeparationPlan",
    "SeparationRecord",
    "SeparationSource",
    "merged_page_count",
    "plan_join_merge",
    "plan_split",
    "validate_partition",
]
