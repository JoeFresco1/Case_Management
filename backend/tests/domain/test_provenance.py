"""Tests for capture provenance + channel/quarantine rules (T1 / FM-027).

Acceptance anchors from ``specs/002-leftovers/spec.md`` T1 and the
FM-027 row (``01-deduplicated-feature-map.md:76``):

1. **Provenance** — every captured item carries source system, source
   id, channel, timestamp, and payload hash.
2. **Per-record quarantine** — a malformed item is isolated without
   failing its batch siblings.
3. **Release / reject rules** — held records resolve with actor +
   reason; terminal records refuse the opposite transition and repeat
   resolutions are idempotent.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. Deterministic: fixed clock, fixed UUIDs, no I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain.channel import (
    CaptureChannel,
    Channel,
    ChannelKind,
    QuarantineRecord,
    ValidationPolicy,
    evaluate_row,
)
from app.domain.errors import InvariantViolation
from app.domain.provenance import (
    Provenance,
    compute_payload_hash,
    is_valid_payload_hash,
)

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
CHANNEL_ID = UUID("33333333-3333-3333-3333-333333333333")
ADMIN_USER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
OTHER_USER = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")

FIXED_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
PAYLOAD = b'{"title": "synthetic intake row"}'


def _held_record(label: str = "row-7.csv:7") -> QuarantineRecord:
    """Build a HELD quarantine record with fixed ids."""
    return QuarantineRecord(
        id=UUID("44444444-4444-4444-4444-444444444444"),
        tenant_id=ACME_TENANT,
        channel_id=CHANNEL_ID,
        channel_key="watched_folder",
        source_label=label,
        captured_at=FIXED_NOW,
        captured_by=ADMIN_USER,
        error_code="missing_required_field",
        error_message="required fields missing: ['title']",
        correlation_id="corr-prov-001",
    )


# ---------------------------------------------------------------------------
# 1. Payload hash
# ---------------------------------------------------------------------------


def test_compute_payload_hash__same_bytes__deterministic_hex() -> None:
    """SHA-256 of the same bytes is stable and 64-char lowercase hex."""
    first = compute_payload_hash(PAYLOAD)
    second = compute_payload_hash(PAYLOAD)
    assert first == second
    assert is_valid_payload_hash(first)


def test_compute_payload_hash__empty_bytes__raises() -> None:
    """Empty captures are caller bugs; they must not mint a receipt."""
    with pytest.raises(InvariantViolation):
        compute_payload_hash(b"")


def test_is_valid_payload_hash__bad_shapes__false() -> None:
    """Truncated, uppercased, or non-hex digests are not valid hashes."""
    assert not is_valid_payload_hash("abc")
    assert not is_valid_payload_hash("G" * 64)
    assert not is_valid_payload_hash("zz" + "a" * 62)


# ---------------------------------------------------------------------------
# 2. Provenance construction
# ---------------------------------------------------------------------------


def test_provenance_create__from_payload__computes_matching_hash() -> None:
    """The factory hash always matches the bytes actually received."""
    prov = Provenance.create(
        source_system="watched_folder",
        source_record_id="row-7.csv:7",
        channel_key="watched_folder",
        payload=PAYLOAD,
        captured_at=FIXED_NOW,
    )
    assert prov.payload_hash == compute_payload_hash(PAYLOAD)
    assert prov.source_system == "watched_folder"
    assert prov.source_record_id == "row-7.csv:7"
    assert prov.channel_key == "watched_folder"
    assert prov.captured_at == FIXED_NOW


def test_provenance__blank_source_system__raises() -> None:
    """A receipt without a source system is meaningless; refuse it."""
    with pytest.raises(InvariantViolation):
        Provenance(
            source_system="  ",
            source_record_id="row-1",
            channel_key="import",
            captured_at=FIXED_NOW,
            payload_hash=compute_payload_hash(PAYLOAD),
        )


def test_provenance__blank_source_record_id__raises() -> None:
    """A receipt without a source id cannot reconcile; refuse it."""
    with pytest.raises(InvariantViolation):
        Provenance(
            source_system="import",
            source_record_id="",
            channel_key="import",
            captured_at=FIXED_NOW,
            payload_hash=compute_payload_hash(PAYLOAD),
        )


def test_provenance__oversized_source_system__raises() -> None:
    """Unbounded source names would bloat the audit trail; cap them."""
    with pytest.raises(InvariantViolation):
        Provenance(
            source_system="s" * 65,
            source_record_id="row-1",
            channel_key="import",
            captured_at=FIXED_NOW,
            payload_hash=compute_payload_hash(PAYLOAD),
        )


def test_provenance__malformed_hash__raises() -> None:
    """A truncated or non-hex digest must not land on a receipt."""
    with pytest.raises(InvariantViolation):
        Provenance(
            source_system="import",
            source_record_id="row-1",
            channel_key="import",
            captured_at=FIXED_NOW,
            payload_hash="not-a-hash",
        )


def test_provenance__naive_timestamp__raises() -> None:
    """Naive timestamps shift meaning across workers; require tz-aware."""
    with pytest.raises(InvariantViolation):
        Provenance(
            source_system="import",
            source_record_id="row-1",
            channel_key="import",
            captured_at=datetime(2026, 9, 1, 12, 0, 0),  # noqa: DTZ001 - naive on purpose
            payload_hash=compute_payload_hash(PAYLOAD),
        )


def test_provenance_matches_payload__same_and_other_bytes__true_then_false() -> None:
    """The receipt verifies the exact bytes; altered bytes do not match."""
    prov = Provenance.create(
        source_system="mailbox",
        source_record_id="<msg-1@example.test>",
        channel_key="mailbox",
        payload=PAYLOAD,
        captured_at=FIXED_NOW,
    )
    assert prov.matches_payload(PAYLOAD)
    assert not prov.matches_payload(b'{"title": "tampered"}')
    assert not prov.matches_payload(b"")


def test_provenance_to_audit_dict__contains_identity_only__no_payload_content() -> None:
    """The audit mapping carries identity + hash, never payload content."""
    prov = Provenance.create(
        source_system="scan",
        source_record_id="batch-3/page-1",
        channel_key="scan",
        payload=PAYLOAD,
        captured_at=FIXED_NOW,
    )
    audit = prov.to_audit_dict()
    assert audit == {
        "source_system": "scan",
        "source_record_id": "batch-3/page-1",
        "channel_key": "scan",
        "captured_at": FIXED_NOW.isoformat(),
        "payload_hash": compute_payload_hash(PAYLOAD),
    }
    # why: the raw payload must never leak into the audit trail
    # (Constitution V); the hash is the verifiable reference instead.
    assert "synthetic intake row" not in str(audit.values())


def test_provenance_for_quarantine__held_record__pins_channel_and_time() -> None:
    """The quarantine receipt agrees with the record on channel + time."""
    record = _held_record()
    prov = Provenance.for_quarantine(record, source_system="import", payload=PAYLOAD)
    assert prov.channel_key == record.channel_key
    assert prov.captured_at == record.captured_at
    assert prov.source_record_id == record.source_label
    assert prov.payload_hash == compute_payload_hash(PAYLOAD)


# ---------------------------------------------------------------------------
# 3. CaptureChannel alias
# ---------------------------------------------------------------------------


def test_capture_channel__is_channel__same_aggregate() -> None:
    """T1 code may name the entry point CaptureChannel; it is Channel."""
    assert CaptureChannel is Channel
    channel = CaptureChannel.create(
        tenant_id=ACME_TENANT,
        key="watched_folder",
        kind=ChannelKind.IMPORT,
        policy=ValidationPolicy(required_fields=("title",)),
        created_by=ADMIN_USER,
        now=FIXED_NOW,
    )
    assert isinstance(channel, Channel)
    assert channel.enabled


def test_capture_channel_disable__blocks_new_captures__in_flight_untouched() -> None:
    """Disabling is explicit and idempotent; history is not rewritten."""
    channel = CaptureChannel.create(
        tenant_id=ACME_TENANT,
        key="mailbox",
        kind=ChannelKind.EMAIL,
        policy=ValidationPolicy(required_fields=("title",)),
        created_by=ADMIN_USER,
        now=FIXED_NOW,
    )
    disabled = channel.disable(at=FIXED_NOW)
    assert not disabled.enabled
    assert disabled.disable(at=FIXED_NOW) == disabled


# ---------------------------------------------------------------------------
# 4. Quarantine release / reject rules
# ---------------------------------------------------------------------------


def test_quarantine_release__held__resolves_with_actor_and_reason() -> None:
    """Release records who resolved the row, when, and why."""
    record = _held_record()
    released = record.release(by=OTHER_USER, at=FIXED_NOW, note="title corrected")
    assert released.status.value == "released"
    assert released.resolved_by == OTHER_USER
    assert released.resolved_at == FIXED_NOW
    assert released.resolution_note == "title corrected"


def test_quarantine_reject__held__terminal_with_actor_and_reason() -> None:
    """Reject records who dropped the row and why; the row stays."""
    record = _held_record()
    rejected = record.reject(by=OTHER_USER, at=FIXED_NOW, note="duplicate of row 1")
    assert rejected.status.value == "rejected"
    assert rejected.resolved_by == OTHER_USER
    assert rejected.resolution_note == "duplicate of row 1"


def test_quarantine_release__already_released__idempotent() -> None:
    """Retried releases (flaky operator connection) change nothing."""
    released = _held_record().release(by=OTHER_USER, at=FIXED_NOW, note="ok")
    assert released.release(by=OTHER_USER, at=FIXED_NOW, note="ok") == released


def test_quarantine_reject__already_rejected__idempotent() -> None:
    """Retried rejects change nothing."""
    rejected = _held_record().reject(by=OTHER_USER, at=FIXED_NOW, note="dup")
    assert rejected.reject(by=OTHER_USER, at=FIXED_NOW, note="dup") == rejected


def test_quarantine_release__rejected_record__raises() -> None:
    """The audit trail is append-only: rejected rows never release."""
    rejected = _held_record().reject(by=OTHER_USER, at=FIXED_NOW, note="dup")
    with pytest.raises(InvariantViolation):
        rejected.release(by=OTHER_USER, at=FIXED_NOW)


def test_quarantine_reject__released_record__raises() -> None:
    """Released rows never reject; the pipeline owns them now."""
    released = _held_record().release(by=OTHER_USER, at=FIXED_NOW, note="ok")
    with pytest.raises(InvariantViolation):
        released.reject(by=OTHER_USER, at=FIXED_NOW)


# ---------------------------------------------------------------------------
# 5. Per-record batch isolation
# ---------------------------------------------------------------------------


def test_evaluate_row__batch_with_malformed_item__isolates_without_failing_batch() -> None:
    """One bad row quarantines; its siblings still validate clean."""
    policy = ValidationPolicy(required_fields=("title",))
    batch = [
        {"title": "good row one"},
        {"summary": "missing title"},
        {"title": "good row two"},
    ]
    decisions = [evaluate_row(values=row, policy=policy) for row in batch]
    assert decisions[0].accepted
    assert decisions[2].accepted
    assert not decisions[1].accepted
    assert decisions[1].error_code == "missing_required_field"
    # why: the operator console buckets on error_code, so the held row
    # must carry the stable code even though its payload values stay
    # out of the diagnostic context (Constitution V).
    assert decisions[1].context == {"missing": ["title"]}
