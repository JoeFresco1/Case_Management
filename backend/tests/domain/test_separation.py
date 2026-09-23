"""Pure-domain tests for the separation workspace (T5 / 002-leftovers).

Covers the rules in ``app.domain.separation``:

- split plans fully partition the source (no gaps, no overlaps, no
  out-of-bounds ranges) and mint one output id per range;
- every plan preserves the page audit trail (source ids + source
  versions + page mapping + actor + reason in the record);
- locked sources refuse with ``DocumentLocked``; cross-tenant sources
  refuse with ``Forbidden`` (fail-closed, checked first);
- join/merge needs >= 2 distinct sources from the same case and
  preserves source order in the record.

Naming follows ``test_<unit>_<scenario>__<expectation>`` (AGENTS.md §4.R5).
Structure is ``// given`` / ``// when`` / ``// then``. Deterministic: fixed
clock timestamp, fixed UUIDs, no I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain.errors import DocumentLocked, Forbidden, ValidationFailed
from app.domain.separation import (
    PageRange,
    SeparationOperation,
    SeparationSource,
    merged_page_count,
    plan_join_merge,
    plan_split,
    validate_partition,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
CASE_ID = UUID("33333333-3333-3333-3333-333333333333")
OTHER_CASE = UUID("44444444-4444-4444-4444-444444444444")
ACTOR_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
DOC_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DOC_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _source(
    *,
    document_id: UUID = DOC_A,
    tenant_id: UUID = ACME_TENANT,
    case_id: UUID = CASE_ID,
    version_number: int = 3,
    page_count: int = 10,
    locked: bool = False,
) -> SeparationSource:
    """Build a source descriptor for separation-plan tests."""
    return SeparationSource(
        document_id=document_id,
        tenant_id=tenant_id,
        case_id=case_id,
        version_number=version_number,
        page_count=page_count,
        locked=locked,
    )


# ---------------------------------------------------------------------------
# PageRange shape
# ---------------------------------------------------------------------------


def test_page_range__inverted_bounds__raises_value_error() -> None:
    """An end page before the start page is a construction bug."""
    # // given an inverted interval
    # // when constructing the range // then ValueError is raised.
    with pytest.raises(ValueError, match="must be >= start_page"):
        PageRange(start_page=5, end_page=4)


def test_page_range__start_below_one__raises_value_error() -> None:
    """Pages are 1-based; page zero does not exist."""
    # // given a zero-based start
    # // when constructing the range // then ValueError is raised.
    with pytest.raises(ValueError, match="must be >= 1"):
        PageRange(start_page=0, end_page=3)


# ---------------------------------------------------------------------------
# Partition validation
# ---------------------------------------------------------------------------


def test_validate_partition__exact_cover__accepts() -> None:
    """Ranges covering 1..N exactly once are a valid partition."""
    # // given two ranges covering pages 1..10 exactly
    ranges = (PageRange(1, 4), PageRange(5, 10))
    # // when validating // then no error is raised.
    validate_partition(page_count=10, ranges=ranges)


def test_validate_partition__gap_between_ranges__rejects() -> None:
    """A gap would silently drop pages from the audit trail."""
    # // given ranges covering 1..3 and 5..10 (page 4 orphaned)
    ranges = (PageRange(1, 3), PageRange(5, 10))
    # // when validating // then ValidationFailed names page_ranges.
    with pytest.raises(ValidationFailed, match="without gaps or overlaps"):
        validate_partition(page_count=10, ranges=ranges)


def test_validate_partition__overlapping_ranges__rejects() -> None:
    """An overlap would duplicate a page across two outputs."""
    # // given ranges overlapping on pages 4..5
    ranges = (PageRange(1, 5), PageRange(4, 10))
    # // when validating // then ValidationFailed names page_ranges.
    with pytest.raises(ValidationFailed, match="without gaps or overlaps"):
        validate_partition(page_count=10, ranges=ranges)


def test_validate_partition__range_past_end__rejects() -> None:
    """A range past the last page references pages that do not exist."""
    # // given a range ending at page 11 of a 10-page source
    ranges = (PageRange(1, 11),)
    # // when validating // then ValidationFailed cites the page count.
    with pytest.raises(ValidationFailed, match="exceeds the source page count"):
        validate_partition(page_count=10, ranges=ranges)


def test_validate_partition__empty_ranges__rejects() -> None:
    """A split with no ranges is a no-op that must not mint a record."""
    # // given no ranges
    # // when validating // then ValidationFailed is raised.
    with pytest.raises(ValidationFailed, match="at least one page range"):
        validate_partition(page_count=10, ranges=())


# ---------------------------------------------------------------------------
# Split plans
# ---------------------------------------------------------------------------


def test_plan_split__valid_geometry__mints_output_per_range() -> None:
    """A valid split mints one output id per range in page order."""
    # // given a 10-page source and an unordered two-range partition
    source = _source()
    ranges = (PageRange(6, 10), PageRange(1, 5))
    # // when planning the split
    plan = plan_split(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        source=source,
        ranges=ranges,
        actor_id=ACTOR_ID,
        reason="separate cover letter from exhibits",
        now=FIXED_NOW,
    )
    # // then two outputs exist in ascending page order with the trail.
    assert len(plan.record.output_document_ids) == 2
    assert plan.output_ranges == (PageRange(1, 5), PageRange(6, 10))
    assert plan.record.operation is SeparationOperation.SPLIT
    assert plan.record.source_document_ids == (DOC_A,)
    assert plan.record.source_versions == (3,)
    assert plan.record.actor_id == ACTOR_ID
    assert plan.record.occurred_at == FIXED_NOW


def test_plan_split__record__preserves_page_audit_trail() -> None:
    """The record alone proves which pages of which version went where."""
    # // given a valid split plan
    plan = plan_split(
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        source=_source(version_number=3),
        ranges=(PageRange(1, 10),),
        actor_id=ACTOR_ID,
        reason="single-output split keeps full history",
        now=FIXED_NOW,
    )
    # // when describing the record // then ids, versions, geometry survive.
    summary = plan.record.describe()
    assert summary["sources"] == [str(DOC_A)]
    assert summary["source_versions"] == [3]
    assert summary["page_ranges"] == [{"start_page": 1, "end_page": 10}]
    outputs = summary["outputs"]
    assert isinstance(outputs, list)
    assert len(outputs) == 1


def test_plan_split__locked_source__refuses_with_document_locked() -> None:
    """A filed document refuses separation before any side effect."""
    # // given a locked (filed) source
    source = _source(locked=True)
    # // when planning // then DocumentLocked is raised.
    with pytest.raises(DocumentLocked, match="is filed"):
        plan_split(
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            source=source,
            ranges=(PageRange(1, 10),),
            actor_id=ACTOR_ID,
            reason="attempt on filed batch",
            now=FIXED_NOW,
        )


def test_plan_split__foreign_tenant_source__denies_fail_closed() -> None:
    """Cross-tenant separation denies even with otherwise-valid input."""
    # // given a source owned by another tenant
    source = _source(tenant_id=OTHER_TENANT)
    # // when planning // then Forbidden carries only a safe reason.
    with pytest.raises(Forbidden) as exc_info:
        plan_split(
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            source=source,
            ranges=(PageRange(1, 10),),
            actor_id=ACTOR_ID,
            reason="cross-tenant attempt",
            now=FIXED_NOW,
        )
    assert exc_info.value.details == {"reason": "cross_tenant"}


def test_plan_split__other_case__rejects() -> None:
    """Split outputs must stay in the source's case."""
    # // given a source from another case
    source = _source(case_id=OTHER_CASE)
    # // when planning into this case // then ValidationFailed names case_id.
    with pytest.raises(ValidationFailed, match="must belong to the source's case"):
        plan_split(
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            source=source,
            ranges=(PageRange(1, 10),),
            actor_id=ACTOR_ID,
            reason="cross-case split attempt",
            now=FIXED_NOW,
        )


def test_plan_split__blank_reason__rejects() -> None:
    """The audit trail needs a justification; blank reasons are refused."""
    # // given a blank reason
    # // when planning // then ValidationFailed names reason.
    with pytest.raises(ValidationFailed, match="reason is required"):
        plan_split(
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            source=_source(),
            ranges=(PageRange(1, 10),),
            actor_id=ACTOR_ID,
            reason="   ",
            now=FIXED_NOW,
        )


# ---------------------------------------------------------------------------
# Join / merge plans
# ---------------------------------------------------------------------------


def test_plan_join_merge__valid_sources__preserves_order_and_versions() -> None:
    """A merge records sources in order with their versions for provenance."""
    # // given two unlocked same-case sources
    sources = (
        _source(document_id=DOC_A, version_number=2, page_count=4),
        _source(document_id=DOC_B, version_number=5, page_count=6),
    )
    # // when planning the merge
    plan = plan_join_merge(
        operation=SeparationOperation.MERGE,
        tenant_id=ACME_TENANT,
        case_id=CASE_ID,
        sources=sources,
        actor_id=ACTOR_ID,
        reason="reassemble exhibits in order",
        now=FIXED_NOW,
    )
    # // then one output id exists and the record keeps order + versions.
    assert len(plan.record.output_document_ids) == 1
    assert plan.record.source_document_ids == (DOC_A, DOC_B)
    assert plan.record.source_versions == (2, 5)
    assert merged_page_count(sources) == 10


def test_plan_join_merge__single_source__rejects_as_silent_fork() -> None:
    """A one-source merge would fork history silently; it is rejected."""
    # // given a single source
    sources = (_source(),)
    # // when planning // then ValidationFailed requires >= 2 sources.
    with pytest.raises(ValidationFailed, match="at least 2 source documents"):
        plan_join_merge(
            operation=SeparationOperation.MERGE,
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            sources=sources,
            actor_id=ACTOR_ID,
            reason="single-source copy attempt",
            now=FIXED_NOW,
        )


def test_plan_join_merge__duplicate_source__rejects() -> None:
    """Listing the same document twice would duplicate its pages."""
    # // given the same source twice
    sources = (_source(document_id=DOC_A), _source(document_id=DOC_A))
    # // when planning // then ValidationFailed names the duplicate.
    with pytest.raises(ValidationFailed, match="listed twice"):
        plan_join_merge(
            operation=SeparationOperation.JOIN,
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            sources=sources,
            actor_id=ACTOR_ID,
            reason="duplicate listing",
            now=FIXED_NOW,
        )


def test_plan_join_merge__mixed_cases__rejects() -> None:
    """Cross-case combination is a relationship decision, not page surgery."""
    # // given sources from two different cases
    sources = (
        _source(document_id=DOC_A, case_id=CASE_ID),
        _source(document_id=DOC_B, case_id=OTHER_CASE),
    )
    # // when planning // then ValidationFailed names sources.
    with pytest.raises(ValidationFailed, match="belongs to another case"):
        plan_join_merge(
            operation=SeparationOperation.MERGE,
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            sources=sources,
            actor_id=ACTOR_ID,
            reason="cross-case merge attempt",
            now=FIXED_NOW,
        )


def test_plan_join_merge__locked_member__refuses_with_document_locked() -> None:
    """Any filed member vetoes the whole join/merge."""
    # // given one locked member among the sources
    sources = (_source(document_id=DOC_A), _source(document_id=DOC_B, locked=True))
    # // when planning // then DocumentLocked is raised.
    with pytest.raises(DocumentLocked, match="is filed"):
        plan_join_merge(
            operation=SeparationOperation.JOIN,
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            sources=sources,
            actor_id=ACTOR_ID,
            reason="join with filed member",
            now=FIXED_NOW,
        )


def test_plan_join_merge__split_operation__rejects() -> None:
    """The join/merge entry point refuses the split operation."""
    # // given the SPLIT operation on the wrong entry point
    sources = (_source(document_id=DOC_A), _source(document_id=DOC_B))
    # // when planning // then ValidationFailed names operation.
    with pytest.raises(ValidationFailed, match="supports join/merge"):
        plan_join_merge(
            operation=SeparationOperation.SPLIT,
            tenant_id=ACME_TENANT,
            case_id=CASE_ID,
            sources=sources,
            actor_id=ACTOR_ID,
            reason="wrong entry point",
            now=FIXED_NOW,
        )
