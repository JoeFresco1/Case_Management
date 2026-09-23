"""Pure-domain tests for the scan-batch inbox (T1 / 002-leftovers).

Covers the rules in ``app.domain.scan_batch``:

- batches open with device/source/channel provenance intact;
- pages append densely numbered and over-cap batches refuse;
- the hold-until-verified gate: ``mark_released`` refuses every status
  except ``VERIFIED``; empty batches cannot become ``READY``;
- tenant isolation fails closed (cross-tenant mutation raises
  ``Forbidden`` before any other check);
- provenance summaries carry ids + device + counts, never storage refs.

Naming follows ``test_<unit>_<scenario>__<expectation>`` (AGENTS.md §4.R5).
Structure is ``// given`` / ``// when`` / ``// then``. Deterministic:
fixed clock timestamps, fixed UUIDs, no I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain.errors import Forbidden, InvariantViolation, ValidationFailed
from app.domain.scan_batch import (
    ScanBatch,
    ScanBatchStatus,
    is_terminal_batch_status,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
CHANNEL_ID = UUID("33333333-3333-3333-3333-333333333333")
ACTOR_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
VERIFIER_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
CASE_ID = UUID("44444444-4444-4444-4444-444444444444")

FIXED_NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)

_SHA = "a" * 64
_SHA_B = "b" * 64


def _open_batch() -> ScanBatch:
    """Open a minimal batch for lifecycle tests."""
    return ScanBatch.open(
        tenant_id=ACME_TENANT,
        channel_id=CHANNEL_ID,
        channel_key="scan-room-1",
        device_id="scanner-07",
        source_label="morning intake tray",
        created_by=ACTOR_ID,
        now=FIXED_NOW,
        case_id=CASE_ID,
    )


def _batch_with_pages(count: int) -> ScanBatch:
    """Open a batch and feed it ``count`` pages."""
    batch = _open_batch()
    for _ in range(count):
        batch = batch.add_page(
            tenant_id=ACME_TENANT,
            sha256=_SHA,
            size=1024,
            storage_ref="ref-1",
            device_id="scanner-07",
            now=FIXED_NOW,
        )
    return batch


# ---------------------------------------------------------------------------
# Provenance on open
# ---------------------------------------------------------------------------


def test_open__fresh_batch__carries_device_and_source_provenance() -> None:
    """A fresh batch pins device, source label, channel, and case."""
    # // given nothing // when opening a batch // then provenance is pinned.
    batch = _open_batch()
    assert batch.status is ScanBatchStatus.OPEN
    assert batch.device_id == "scanner-07"
    assert batch.source_label == "morning intake tray"
    assert batch.channel_key == "scan-room-1"
    assert batch.channel_id == CHANNEL_ID
    assert batch.case_id == CASE_ID
    assert batch.pages == ()
    provenance = batch.describe_provenance()
    assert provenance["device_id"] == "scanner-07"
    assert provenance["channel_key"] == "scan-room-1"
    assert "storage_ref" not in provenance
    assert "ref-1" not in str(provenance.values())


def test_open__blank_device__raises_invariant() -> None:
    """A batch without a device id cannot exist (provenance is mandatory)."""
    # // given an empty device id // when opening // then InvariantViolation.
    with pytest.raises(InvariantViolation, match="device_id"):
        ScanBatch.open(
            tenant_id=ACME_TENANT,
            channel_id=CHANNEL_ID,
            channel_key="scan-room-1",
            device_id="",
            source_label="tray",
            created_by=ACTOR_ID,
            now=FIXED_NOW,
        )


# ---------------------------------------------------------------------------
# Page intake
# ---------------------------------------------------------------------------


def test_add_page__two_pages__numbers_dense_from_one() -> None:
    """Pages number densely so the batch is partition-ready for separation."""
    # // given an open batch // when adding two pages // then 1..2 dense.
    batch = _batch_with_pages(2)
    assert [p.page_number for p in batch.pages] == [1, 2]
    assert batch.page_count == 2


def test_add_page__bad_digest__raises_before_persistence() -> None:
    """A page without a valid digest refuses before any row is written."""
    # // given an open batch // when adding a digest-less page // then refused.
    batch = _open_batch()
    with pytest.raises(InvariantViolation, match="sha256"):
        batch.add_page(
            tenant_id=ACME_TENANT,
            sha256="not-a-digest",
            size=10,
            storage_ref="ref-x",
            device_id="scanner-07",
            now=FIXED_NOW,
        )


def test_add_page__cross_tenant__raises_forbidden_first() -> None:
    """Cross-tenant page intake fails closed even on a valid batch."""
    # // given a batch owned by ACME // when another tenant adds // then 403-shape.
    batch = _open_batch()
    with pytest.raises(Forbidden, match="cross-tenant"):
        batch.add_page(
            tenant_id=OTHER_TENANT,
            sha256=_SHA,
            size=10,
            storage_ref="ref-x",
            device_id="scanner-07",
            now=FIXED_NOW,
        )


def test_add_page__after_ready__refuses_closed_batch() -> None:
    """Pages cannot land after intake was declared complete."""
    # // given a ready batch // when adding a page // then ValidationFailed.
    batch = _batch_with_pages(1).mark_ready(tenant_id=ACME_TENANT, now=FIXED_NOW)
    with pytest.raises(ValidationFailed, match="only open batches accept pages"):
        batch.add_page(
            tenant_id=ACME_TENANT,
            sha256=_SHA_B,
            size=10,
            storage_ref="ref-x",
            device_id="scanner-07",
            now=FIXED_NOW,
        )


# ---------------------------------------------------------------------------
# Hold-until-verified gate
# ---------------------------------------------------------------------------


def test_mark_ready__empty_batch__refuses_misfeed() -> None:
    """An empty batch is a scanner misfeed, not a verifiable unit."""
    # // given an empty batch // when marking ready // then refused.
    with pytest.raises(ValidationFailed, match="no pages"):
        _open_batch().mark_ready(tenant_id=ACME_TENANT, now=FIXED_NOW)


def test_verify__ready_batch__records_human_verifier() -> None:
    """Verification pins the human verifier and timestamp."""
    # // given a ready batch // when a human verifies // then VERIFIED.
    batch = _batch_with_pages(1).mark_ready(tenant_id=ACME_TENANT, now=FIXED_NOW)
    verified = batch.verify(tenant_id=ACME_TENANT, by=VERIFIER_ID, now=FIXED_NOW)
    assert verified.status is ScanBatchStatus.VERIFIED
    assert verified.verified_by == VERIFIER_ID
    assert verified.verified_at == FIXED_NOW


def test_verify__open_batch__refuses_skip_of_ready() -> None:
    """Verification cannot skip the ready declaration."""
    # // given an open (never-ready) batch // when verifying // then refused.
    with pytest.raises(ValidationFailed, match="only ready batches"):
        _batch_with_pages(1).verify(tenant_id=ACME_TENANT, by=VERIFIER_ID, now=FIXED_NOW)


def test_mark_released__unverified_batch__refuses_filing_bypass() -> None:
    """Filing from an unverified batch is the exact bypass the gate prevents."""
    # // given a ready-but-unverified batch // when releasing // then refused.
    batch = _batch_with_pages(1).mark_ready(tenant_id=ACME_TENANT, now=FIXED_NOW)
    with pytest.raises(ValidationFailed, match="must be verified"):
        batch.mark_released(tenant_id=ACME_TENANT, now=FIXED_NOW)


def test_mark_released__verified_batch__reaches_terminal() -> None:
    """The full open -> ready -> verified -> released path commits."""
    # // given a verified batch // when releasing // then RELEASED + terminal.
    batch = (
        _batch_with_pages(1)
        .mark_ready(tenant_id=ACME_TENANT, now=FIXED_NOW)
        .verify(tenant_id=ACME_TENANT, by=VERIFIER_ID, now=FIXED_NOW)
        .mark_released(tenant_id=ACME_TENANT, now=FIXED_NOW)
    )
    assert batch.status is ScanBatchStatus.RELEASED
    assert is_terminal_batch_status(batch.status)


def test_reject__released_batch__raises_invariant() -> None:
    """A released batch cannot be rejected — history is append-only."""
    # // given a released batch // when rejecting // then InvariantViolation.
    batch = (
        _batch_with_pages(1)
        .mark_ready(tenant_id=ACME_TENANT, now=FIXED_NOW)
        .verify(tenant_id=ACME_TENANT, by=VERIFIER_ID, now=FIXED_NOW)
        .mark_released(tenant_id=ACME_TENANT, now=FIXED_NOW)
    )
    with pytest.raises(InvariantViolation, match="released"):
        batch.reject(tenant_id=ACME_TENANT, now=FIXED_NOW)


def test_verify__cross_tenant__raises_forbidden() -> None:
    """Cross-tenant verification fails closed."""
    # // given ACME's ready batch // when another tenant verifies // then denied.
    batch = _batch_with_pages(1).mark_ready(tenant_id=ACME_TENANT, now=FIXED_NOW)
    with pytest.raises(Forbidden, match="cross-tenant"):
        batch.verify(tenant_id=OTHER_TENANT, by=VERIFIER_ID, now=FIXED_NOW)


# ---------------------------------------------------------------------------
# Separation projection preserves provenance (T1)
# ---------------------------------------------------------------------------


def test_to_separation_source__verified_batch__preserves_per_page_provenance() -> None:
    """Each page projects with its sha/device/batch/channel provenance."""
    # // given a verified two-page batch with distinct digests.
    batch = _open_batch()
    batch = batch.add_page(
        tenant_id=ACME_TENANT,
        sha256=_SHA,
        size=1024,
        storage_ref="ref-1",
        device_id="scanner-07",
        now=FIXED_NOW,
    )
    batch = batch.add_page(
        tenant_id=ACME_TENANT,
        sha256=_SHA_B,
        size=2048,
        storage_ref="ref-2",
        device_id="scanner-07",
        now=FIXED_NOW,
    )
    batch = batch.mark_ready(tenant_id=ACME_TENANT, now=FIXED_NOW).verify(
        tenant_id=ACME_TENANT, by=VERIFIER_ID, now=FIXED_NOW
    )
    # // when projecting to separation sources.
    sources = batch.to_separation_source()
    # // then per-page provenance survives without bytes.
    assert len(sources) == 2
    assert [s.page_number for s in sources] == [1, 2]
    assert sources[0].sha256 == _SHA
    assert sources[1].sha256 == _SHA_B
    assert all(s.device_id == "scanner-07" for s in sources)
    assert all(s.batch_id == batch.id for s in sources)
    assert all(s.channel_key == "scan-room-1" for s in sources)
    assert all(s.source_label == "morning intake tray" for s in sources)
    assert sources[0].storage_ref == "ref-1"
    # // and the audit mapping carries identity only.
    audit = sources[0].to_audit_dict()
    assert audit["sha256"] == _SHA
    assert audit["batch_id"] == str(batch.id)


def test_to_separation_source__ready_batch__allowed_for_triage() -> None:
    """A ready (intake-complete, unverified) batch may triage geometry."""
    # // given a ready batch // when projecting // then descriptors return.
    batch = _batch_with_pages(1).mark_ready(tenant_id=ACME_TENANT, now=FIXED_NOW)
    sources = batch.to_separation_source()
    assert len(sources) == 1
    assert sources[0].batch_id == batch.id


def test_to_separation_source__open_batch__refuses_unverified() -> None:
    """An open batch must not enter separation (hold-until-verified)."""
    # // given an open batch // when projecting // then ValidationFailed on status.
    batch = _batch_with_pages(1)
    with pytest.raises(ValidationFailed, match="only ready or verified"):
        batch.to_separation_source()


def test_as_document_separation_source__verified_batch__projects_single_source() -> None:
    """The whole-batch projection feeds document-level split planning."""
    # // given a verified batch // when projecting as a document source.
    batch = (
        _batch_with_pages(2)
        .mark_ready(tenant_id=ACME_TENANT, now=FIXED_NOW)
        .verify(tenant_id=ACME_TENANT, by=VERIFIER_ID, now=FIXED_NOW)
    )
    source = batch.as_document_separation_source()
    # // then the single source pins batch identity + page count.
    assert source.document_id == batch.id
    assert source.tenant_id == ACME_TENANT
    assert source.case_id == CASE_ID
    assert source.page_count == 2
    assert source.locked is False


def test_describe_provenance__has_no_storage_ref() -> None:
    """Batch provenance summaries never embed storage refs."""
    # // given a batch with pages // when describing // then no storage_ref.
    batch = _batch_with_pages(1)
    provenance = batch.describe_provenance()
    assert "storage_ref" not in provenance
    assert "ref-1" not in str(provenance.values())
