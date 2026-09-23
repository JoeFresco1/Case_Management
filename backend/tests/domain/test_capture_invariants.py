"""Domain invariants for capture ingestion (T1 / FM-026, FM-027).

Acceptance anchors from ``specs/002-leftovers/spec.md`` T1 US1-US5:

1. **Dry-run vs commit** — preview moves ``PENDING`` -> ``PREVIEWED``
   without writing cases; commit moves ``PREVIEWED`` -> ``COMMITTING``
   -> ``COMMITTED``; wrong-state transitions and reconciliation
   mismatches raise; ``FAILED`` is terminal and never follows
   ``COMMITTED``.
2. **Per-record quarantine** — one malformed row in a batch isolates
   with ``missing_required_field`` while siblings stay accepted; the
   diagnostic context carries keys, never payload values.
3. **Source-record idempotency** — every row result carries its
   ``source_id`` for operator correlation; a batch-duplicate shape
   (1 accepted + 1 rejected sharing one id) reconciles cleanly. Full
   duplicate *detection* lives in ``app.application.imports.source_ids``
   and is covered by ``tests/application/test_import_idempotency.py`` —
   it is not re-implemented here.
4. **Provenance** — the receipt hash is pinned at creation; tampered or
   empty bytes fail verification; the quarantine receipt agrees with
   the held record on channel + timestamp.
5. **Tenant boundaries** — repository-shaped ``get(tenant_id, ...)``
   fakes return ``None`` cross-tenant for channels, quarantine rows,
   and import jobs, so a caller holding another tenant's id learns
   nothing (Constitution V).

Evidence matrix ( Rasul: reuse, do not rebuild):

- Reused as-is: ``tests/domain/test_provenance.py`` (hash determinism,
  blank/oversized/malformed/naive guards, audit-dict redaction,
  release/reject idempotency + opposite-transition refusal),
  ``tests/application/test_import.py`` (dry-run non-mutating, commit
  creates, reconciliation matches, cross-tenant denied),
  ``tests/application/test_import_idempotency.py`` (zero-write preview,
  batch duplicates, double-commit idempotent),
  ``tests/application/test_quarantine.py`` (isolate/inspect/release/
  reject/auth), ``tests/domain/test_scan_batch.py`` (hold-until-
  verified gate, cross-tenant ``Forbidden``).
- Added here: the pure-domain state machine (``ImportJob`` preview /
  committing / committed / failed), ``MappingVersion`` /
  ``FieldMapping`` / ``DocumentMapping`` / ``ImportReconciliation``
  guards, ``ValidationPolicy`` / ``PolicyDecision`` / ``Channel`` key
  + policy-update guards, ``QuarantineRecord`` context bans, and the
  tenant-scoped fake-repo proofs for channel / quarantine / import job.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5 with ``// given`` / ``// when`` / ``// then`` markers.
Deterministic: fixed clock, fixed UUIDs, no I/O. Synthetic only: ids
use the ``example.test`` domain; no PHI anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.domain.channel import (
    CaptureChannel,
    Channel,
    ChannelKind,
    PolicyDecision,
    QuarantineRecord,
    ValidationPolicy,
    evaluate_row,
)
from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.import_job import (
    DocumentMapping,
    FieldMapping,
    ImportJob,
    ImportJobStatus,
    ImportPackage,
    ImportReconciliation,
    ImportRowOutcome,
    ImportRowResult,
    MappingVersion,
    is_terminal_import_status,
)
from app.domain.provenance import Provenance, compute_payload_hash

ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
CHANNEL_ID = UUID("33333333-3333-3333-3333-333333333333")
ADMIN_USER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
OTHER_USER = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")

FIXED_NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
PAYLOAD = b'{"title": "synthetic intake row"}'


def _make_mapping() -> MappingVersion:
    """Build the canonical test mapping (id column + title/summary/priority)."""
    return MappingVersion(
        version=1,
        case_type_key="ServiceRequest",
        fields=(
            FieldMapping(source="request_id", target="requestId"),
            FieldMapping(source="title", target="title"),
            FieldMapping(source="summary", target="summary"),
            FieldMapping(source="priority", target="priority"),
        ),
        title_field="title",
        description_field="summary",
        priority_field="priority",
        source_id_field="request_id",
    )


def _make_job() -> ImportJob:
    """Create a fresh PENDING job owned by ACME."""
    # // given the canonical mapping // when creating // then PENDING.
    return ImportJob.create(
        tenant_id=ACME_TENANT,
        source_label="synthetic-package",
        source_format="csv",
        mapping=_make_mapping(),
        created_by=ADMIN_USER,
        now=FIXED_NOW,
        correlation_id="corr-cap-001",
    )


def _accepted(index: int, source_id: str | None = None) -> ImportRowResult:
    """Build an ACCEPTED row result with an optional source id."""
    return ImportRowResult(row_index=index, outcome=ImportRowOutcome.ACCEPTED, source_id=source_id)


def _rejected(index: int, source_id: str | None = None) -> ImportRowResult:
    """Build a REJECTED row result carrying the stable quarantine code."""
    return ImportRowResult(
        row_index=index,
        outcome=ImportRowOutcome.REJECTED,
        source_id=source_id,
        error_code="missing_required_field",
        error_message="required fields missing: ['title']",
    )


def _previewed_job() -> ImportJob:
    """Drive a job to PREVIEWED with a 2-accepted / 1-rejected report."""
    # // given a fresh job // when previewed // then PREVIEWED.
    results = (_accepted(1, "SR-0001"), _accepted(2, "SR-0002"), _rejected(3, "SR-0003"))
    return _make_job().attach_preview(
        source_row_count=3,
        results=results,
        reconciliation=ImportReconciliation(committed_count=2, rejected_count=1),
        now=FIXED_NOW,
    )


def _held_record() -> QuarantineRecord:
    """Build a HELD quarantine record with fixed ids."""
    return QuarantineRecord(
        id=UUID("44444444-4444-4444-4444-444444444444"),
        tenant_id=ACME_TENANT,
        channel_id=CHANNEL_ID,
        channel_key="import-main",
        source_label="row-7.csv:7",
        captured_at=FIXED_NOW,
        captured_by=ADMIN_USER,
        error_code="missing_required_field",
        error_message="required fields missing: ['title']",
        correlation_id="corr-cap-002",
    )


# ---------------------------------------------------------------------------
# 1. ImportPackage alias + dry-run vs commit states
# ---------------------------------------------------------------------------


def test_import_package__is_import_job__same_aggregate() -> None:
    """The T1 spec name is the same aggregate, not a parallel table."""
    # // given the spec entity name // when compared // then identity holds.
    assert ImportPackage is ImportJob
    assert isinstance(_make_job(), ImportPackage)


def test_import_job_create__fresh__pending_with_empty_report() -> None:
    """A fresh job pins tenant + mapping and starts PENDING with no report."""
    # // given nothing // when creating // then PENDING, empty, non-terminal.
    job = _make_job()
    assert job.status is ImportJobStatus.PENDING
    assert job.tenant_id == ACME_TENANT
    assert job.results == ()
    assert job.reconciliation is None
    assert job.source_row_count == 0
    assert not is_terminal_import_status(job.status)


def test_import_job_status__terminal__only_committed_and_failed() -> None:
    """Only COMMITTED and FAILED end the lifecycle; the rest stay open."""
    # // given every lifecycle value // when classified // then terminal split.
    assert is_terminal_import_status(ImportJobStatus.COMMITTED)
    assert is_terminal_import_status(ImportJobStatus.FAILED)
    assert not is_terminal_import_status(ImportJobStatus.PENDING)
    assert not is_terminal_import_status(ImportJobStatus.PREVIEWED)
    assert not is_terminal_import_status(ImportJobStatus.COMMITTING)


def test_import_job_attach_preview__pending__becomes_previewed() -> None:
    """Dry-run attaches the report and parks the job at PREVIEWED."""
    # // given a PENDING job // when previewed // then PREVIEWED with counts.
    job = _previewed_job()
    assert job.status is ImportJobStatus.PREVIEWED
    assert job.reconciliation is not None
    assert job.reconciliation.committed_count == 2
    assert job.reconciliation.rejected_count == 1
    assert job.source_row_count == 3


def test_import_job_attach_preview__repeat__raises() -> None:
    """A previewed job cannot be re-previewed; operators open a new job."""
    # // given a PREVIEWED job // when previewed again // then refused.
    job = _previewed_job()
    with pytest.raises(InvariantViolation, match="preview"):
        job.attach_preview(
            source_row_count=3,
            results=job.results,
            reconciliation=job.reconciliation,
            now=FIXED_NOW,
        )


def test_import_job_attach_preview__counts_mismatch__raises() -> None:
    """A report whose counts disagree with its rows never attaches."""
    # // given rows counting 1 accepted // when reconciliation claims 2
    # // then refused before any state changes.
    job = _make_job()
    with pytest.raises(InvariantViolation, match="reconciliation"):
        job.attach_preview(
            source_row_count=1,
            results=(_accepted(1),),
            reconciliation=ImportReconciliation(committed_count=2, rejected_count=0),
            now=FIXED_NOW,
        )


def test_import_job_mark_committing__previewed__becomes_committing() -> None:
    """Commit starts only from a reviewed preview (dry-run first)."""
    # // given a PREVIEWED job // when committing starts // then COMMITTING.
    job = _previewed_job().mark_committing()
    assert job.status is ImportJobStatus.COMMITTING


def test_import_job_mark_committing__wrong_state__raises() -> None:
    """Committing from PENDING (or twice) skips review and is refused."""
    # // given a PENDING job // when committing // then refused.
    with pytest.raises(InvariantViolation, match="committing"):
        _make_job().mark_committing()
    # // given an already-COMMITTING job // when committing // then refused.
    with pytest.raises(InvariantViolation, match="committing"):
        _previewed_job().mark_committing().mark_committing()


def test_import_job_mark_committed__committing__becomes_committed() -> None:
    """The commit lands with the final reconciliation and timestamp."""
    # // given a COMMITTING job // when committed // then COMMITTED.
    previewed = _previewed_job()
    committing = previewed.mark_committing()
    committed = committing.mark_committed(
        results=previewed.results,
        reconciliation=previewed.reconciliation,
        at=FIXED_NOW,
    )
    assert committed.status is ImportJobStatus.COMMITTED
    assert committed.committed_at == FIXED_NOW
    assert is_terminal_import_status(committed.status)


def test_import_job_mark_committed__wrong_state__raises() -> None:
    """Committing straight from PREVIEWED skips the in-flight gate."""
    # // given a PREVIEWED (never-COMMITTING) job // when committed
    # // then refused.
    previewed = _previewed_job()
    with pytest.raises(InvariantViolation, match="commit"):
        previewed.mark_committed(
            results=previewed.results,
            reconciliation=previewed.reconciliation,
            at=FIXED_NOW,
        )


def test_import_job_mark_committed__counts_mismatch__raises() -> None:
    """The final report must reconcile, like the preview did."""
    # // given a COMMITTING job // when the final counts disagree
    # // then refused.
    committing = _previewed_job().mark_committing()
    with pytest.raises(InvariantViolation, match="reconciliation"):
        committing.mark_committed(
            results=(_accepted(1),),
            reconciliation=ImportReconciliation(committed_count=9, rejected_count=0),
            at=FIXED_NOW,
        )


def test_import_job_mark_failed__committed__refuses() -> None:
    """A committed job is terminal history; failure cannot rewrite it."""
    # // given a COMMITTED job // when failed // then refused.
    previewed = _previewed_job()
    committed = previewed.mark_committing().mark_committed(
        results=previewed.results,
        reconciliation=previewed.reconciliation,
        at=FIXED_NOW,
    )
    with pytest.raises(InvariantViolation, match="committed"):
        committed.mark_failed(reason="late operator abort", at=FIXED_NOW)


def test_import_job_mark_failed__empty_reason__raises() -> None:
    """A failure without a reason is unauditable, so it is refused."""
    # // given a PENDING job // when failed without reason // then refused.
    with pytest.raises(ValidationFailed, match="failure_reason"):
        _make_job().mark_failed(reason="", at=FIXED_NOW)


# ---------------------------------------------------------------------------
# 2. MappingVersion + field/document/reconciliation guards
# ---------------------------------------------------------------------------


def test_mapping_version__valid__exposes_field_lookup() -> None:
    """The canonical mapping resolves its columns and pins its pointers."""
    # // given the canonical mapping // when looked up // then entries found.
    mapping = _make_mapping()
    assert mapping.version == 1
    assert mapping.case_type_key == "ServiceRequest"
    assert mapping.field_for("title") == FieldMapping(source="title", target="title")
    assert mapping.field_for("nope") is None


def test_mapping_version__version_below_one__raises() -> None:
    """Versions start at 1 so re-runs can pin the exact contract."""
    # // given version 0 // when built // then refused.
    with pytest.raises(InvariantViolation, match="version"):
        MappingVersion(
            version=0,
            case_type_key="ServiceRequest",
            fields=(FieldMapping(source="title", target="title"),),
        )


def test_mapping_version__empty_fields__raises() -> None:
    """A mapping with no fields maps nothing, so it cannot exist."""
    # // given zero fields // when built // then refused.
    with pytest.raises(InvariantViolation, match="at least one field"):
        MappingVersion(version=1, case_type_key="ServiceRequest", fields=())


def test_mapping_version__duplicate_source__raises() -> None:
    """Duplicate source columns would silently shadow a column."""
    # // given two entries for one column // when built // then refused.
    with pytest.raises(InvariantViolation, match="duplicate"):
        MappingVersion(
            version=1,
            case_type_key="ServiceRequest",
            fields=(
                FieldMapping(source="title", target="title"),
                FieldMapping(source="title", target="summary"),
            ),
        )


def test_mapping_version__pointer_outside_map__raises() -> None:
    """Title/source-id pointers must name a mapped column."""
    # // given a title pointer outside the map // when built // then refused.
    with pytest.raises(InvariantViolation, match="title_field"):
        MappingVersion(
            version=1,
            case_type_key="ServiceRequest",
            fields=(FieldMapping(source="title", target="title"),),
            title_field="headline",
        )
    # // given a source-id pointer outside the map // when built
    # // then refused (same rule as title/description/priority).
    with pytest.raises(InvariantViolation, match="source_id_field"):
        MappingVersion(
            version=1,
            case_type_key="ServiceRequest",
            fields=(FieldMapping(source="title", target="title"),),
            title_field="title",
            source_id_field="request_id",
        )


def test_field_mapping__empty_source_or_target__raises() -> None:
    """Both ends of a translation are required for traceability."""
    # // given an empty source // when built // then refused.
    with pytest.raises(InvariantViolation, match="source"):
        FieldMapping(source="", target="title")
    # // given an empty target // when built // then refused.
    with pytest.raises(InvariantViolation, match="target"):
        FieldMapping(source="title", target="")


def test_document_mapping__empty_attr__raises() -> None:
    """A document extractor naming no column would silently drop files."""
    # // given an empty file-name column // when built // then refused.
    with pytest.raises(InvariantViolation, match="file_name_field"):
        DocumentMapping(
            file_name_field="",
            content_field="content",
            content_type_field="content_type",
            classification_field="classification",
            sha256_field="sha256",
        )


def test_import_job__unsupported_format__raises() -> None:
    """Only the declared bulk formats enter the pipeline (csv/json/jsonl)."""
    # // given an xlsx format // when creating // then fail-closed.
    with pytest.raises(ValidationFailed, match="source_format"):
        ImportJob.create(
            tenant_id=ACME_TENANT,
            source_label="synthetic-package",
            source_format="xlsx",
            mapping=_make_mapping(),
            created_by=ADMIN_USER,
            now=FIXED_NOW,
            correlation_id="corr-cap-003",
        )


def test_import_reconciliation__negative_counts__raises() -> None:
    """Counts are cardinalities, never negative."""
    # // given a negative committed count // when built // then refused.
    with pytest.raises(InvariantViolation, match="committed_count"):
        ImportReconciliation(committed_count=-1, rejected_count=0)
    # // given a negative rejected count // when built // then refused.
    with pytest.raises(InvariantViolation, match="rejected_count"):
        ImportReconciliation(committed_count=0, rejected_count=-1)


# ---------------------------------------------------------------------------
# 3. Per-record quarantine (CaptureChannel + policy + record guards)
# ---------------------------------------------------------------------------


def test_capture_channel__is_channel__same_aggregate() -> None:
    """The T1 track name is the same aggregate, not a fork."""
    # // given the spec entity name // when compared // then identity holds.
    assert CaptureChannel is Channel


def test_evaluate_row__three_row_batch__isolates_bad_row() -> None:
    """One malformed row quarantines; its siblings still validate clean."""
    # // given a 3-row batch with one title-less row // when each row is
    # // evaluated // then the bad row isolates with the stable code.
    policy = ValidationPolicy(required_fields=("title", "summary"))
    batch = [
        {"title": "good row one", "summary": "s1"},
        {"summary": "missing title"},
        {"title": "good row two", "summary": "s3"},
    ]
    decisions = [evaluate_row(values=row, policy=policy) for row in batch]
    assert decisions[0].accepted
    assert decisions[2].accepted
    assert not decisions[1].accepted
    assert decisions[1].error_code == "missing_required_field"
    assert decisions[1].context == {"missing": ["title"]}


def test_evaluate_row__context__carries_keys_not_values() -> None:
    """Diagnostics name the missing keys; payload values never land there."""
    # // given a row missing title with a sensitive-looking summary
    # // when evaluated // then the context holds keys only.
    policy = ValidationPolicy(required_fields=("title",))
    decision = evaluate_row(values={"summary": "Secret Title Value"}, policy=policy)
    assert not decision.accepted
    assert decision.context == {"missing": ["title"]}
    assert "Secret Title Value" not in str(decision.context.values())


def test_quarantine_record__payload_keys__refused() -> None:
    """The review row carries diagnostics, never payload data (V)."""
    # // given each banned context key // when a record is built
    # // then refused before it can persist.
    for banned in ("payload", "body", "raw", "content"):
        context: dict[str, Any] = {banned: "value"}
        with pytest.raises(InvariantViolation, match="context"):
            QuarantineRecord(
                id=UUID("55555555-5555-5555-5555-555555555555"),
                tenant_id=ACME_TENANT,
                channel_id=CHANNEL_ID,
                channel_key="import-main",
                source_label="row-9.csv:9",
                captured_at=FIXED_NOW,
                captured_by=ADMIN_USER,
                error_code="missing_required_field",
                error_message="required fields missing",
                correlation_id="corr-cap-004",
                context=context,
            )


def test_quarantine_record__empty_identity__refused() -> None:
    """A held row without a code or label cannot be triaged."""
    # // given an empty error code // when built // then refused.
    with pytest.raises(InvariantViolation, match="error_code"):
        QuarantineRecord(
            id=UUID("55555555-5555-5555-5555-555555555555"),
            tenant_id=ACME_TENANT,
            channel_id=CHANNEL_ID,
            channel_key="import-main",
            source_label="row-9.csv:9",
            captured_at=FIXED_NOW,
            captured_by=ADMIN_USER,
            error_code="",
            error_message="required fields missing",
            correlation_id="corr-cap-004",
        )
    # // given an empty source label // when built // then refused.
    with pytest.raises(InvariantViolation, match="source_label"):
        QuarantineRecord(
            id=UUID("55555555-5555-5555-5555-555555555555"),
            tenant_id=ACME_TENANT,
            channel_id=CHANNEL_ID,
            channel_key="import-main",
            source_label="",
            captured_at=FIXED_NOW,
            captured_by=ADMIN_USER,
            error_code="missing_required_field",
            error_message="required fields missing",
            correlation_id="corr-cap-004",
        )


def test_validation_policy__bad_caps__refused() -> None:
    """Zero/negative byte caps and shadowed required fields cannot exist."""
    # // given a zero byte cap // when built // then refused.
    with pytest.raises(InvariantViolation, match="max_record_bytes"):
        ValidationPolicy(max_record_bytes=0)
    # // given duplicate required fields // when built // then refused.
    with pytest.raises(InvariantViolation, match="duplicate"):
        ValidationPolicy(required_fields=("title", "title"))


def test_policy_decision__bad_shapes__refused() -> None:
    """Accepted rows carry no error; rejected rows must name one."""
    # // given an accepted row with an error code // when built // then refused.
    with pytest.raises(InvariantViolation, match="error"):
        PolicyDecision(accepted=True, error_code="missing_required_field")
    # // given a rejected row without a code // when built // then refused.
    with pytest.raises(InvariantViolation, match="error_code"):
        PolicyDecision(accepted=False)


def test_channel__key_and_policy__guards_and_update() -> None:
    """Keys stay URL-safe; policy swaps apply to new captures only."""
    # // given an empty key // when creating // then refused.
    with pytest.raises(InvariantViolation, match="key"):
        Channel.create(
            tenant_id=ACME_TENANT,
            key="",
            kind=ChannelKind.IMPORT,
            policy=ValidationPolicy(required_fields=("title",)),
            created_by=ADMIN_USER,
            now=FIXED_NOW,
        )
    # // given a key with spaces // when creating // then refused.
    with pytest.raises(InvariantViolation, match="key"):
        Channel.create(
            tenant_id=ACME_TENANT,
            key="bad key!",
            kind=ChannelKind.IMPORT,
            policy=ValidationPolicy(required_fields=("title",)),
            created_by=ADMIN_USER,
            now=FIXED_NOW,
        )
    # // given a live channel // when the policy is swapped // then the new
    # // policy applies forward while the identity (id/tenant/key) is kept.
    channel = Channel.create(
        tenant_id=ACME_TENANT,
        key="import-main",
        kind=ChannelKind.IMPORT,
        policy=ValidationPolicy(required_fields=("title",)),
        created_by=ADMIN_USER,
        now=FIXED_NOW,
    )
    swapped = channel.update_policy(
        policy=ValidationPolicy(required_fields=("title", "summary")), at=FIXED_NOW
    )
    assert swapped.id == channel.id
    assert swapped.tenant_id == ACME_TENANT
    assert swapped.policy.required_fields == ("title", "summary")


# ---------------------------------------------------------------------------
# 4. Source-record idempotency at the domain level (carriers + shape)
# ---------------------------------------------------------------------------


def test_import_row_result__duplicate_source_ids__carried_for_correlation() -> None:
    """Two rows sharing one source id carry the identical carrier string."""
    # // given two results sharing SR-0001 // when compared // then the
    # // carriers match so the operator (and the application overlay) can
    # // correlate the duplicate without re-reading the payload.
    first = _accepted(1, "SR-0001")
    repeat = ImportRowResult(
        row_index=3,
        outcome=ImportRowOutcome.REJECTED,
        source_id="SR-0001",
        error_code="duplicate_source_id",
        error_message="duplicate source-record id 'SR-0001' within this package",
    )
    assert first.source_id == "SR-0001"
    assert repeat.source_id == first.source_id
    assert repeat.error_code == "duplicate_source_id"


def test_import_job_attach_preview__duplicate_shape__counts_reconcile() -> None:
    """A 1-accepted + 1-rejected duplicate pair previews as (1, 1)."""
    # // given the batch-duplicate shape the application overlay emits
    # // when previewed // then the domain accepts the counts without
    # // implementing detection itself (detection stays in source_ids.py).
    pair = (
        _accepted(1, "SR-0001"),
        ImportRowResult(
            row_index=2,
            outcome=ImportRowOutcome.REJECTED,
            source_id="SR-0001",
            error_code="duplicate_source_id",
            error_message="duplicate source-record id 'SR-0001' within this package",
        ),
    )
    job = _make_job().attach_preview(
        source_row_count=2,
        results=pair,
        reconciliation=ImportReconciliation(committed_count=1, rejected_count=1),
        now=FIXED_NOW,
    )
    assert job.status is ImportJobStatus.PREVIEWED
    assert job.reconciliation is not None
    assert job.reconciliation.committed_count == 1
    assert job.reconciliation.rejected_count == 1


# ---------------------------------------------------------------------------
# 5. Provenance pinning
# ---------------------------------------------------------------------------


def test_provenance_create__pins_hash__tampered_fails() -> None:
    """The receipt verifies the exact bytes; anything else fails."""
    # // given a receipt for the inbound bytes // when the bytes change
    # // then verification fails; empty bytes never verify either.
    prov = Provenance.create(
        source_system="import",
        source_record_id="<row-7@example.test>",
        channel_key="import-main",
        payload=PAYLOAD,
        captured_at=FIXED_NOW,
    )
    assert prov.payload_hash == compute_payload_hash(PAYLOAD)
    assert prov.matches_payload(PAYLOAD)
    assert not prov.matches_payload(b'{"title": "tampered"}')
    assert not prov.matches_payload(b"")


def test_provenance_for_quarantine__pins_channel_and_time() -> None:
    """The quarantine receipt agrees with the record on channel + time."""
    # // given a HELD record // when its receipt is built // then channel,
    # // timestamp, and source label agree so audit and queue cannot drift.
    record = _held_record()
    prov = Provenance.for_quarantine(record, source_system="import", payload=PAYLOAD)
    assert prov.channel_key == record.channel_key
    assert prov.captured_at == record.captured_at
    assert prov.source_record_id == record.source_label
    assert prov.payload_hash == compute_payload_hash(PAYLOAD)


# ---------------------------------------------------------------------------
# 6. Tenant boundaries via repository-shaped fakes (port contract proof)
# ---------------------------------------------------------------------------


class _ChannelStore:
    """Minimal ``ChannelRepository`` shape: tenant-first reads, None across."""

    def __init__(self) -> None:
        self._rows: dict[UUID, Channel] = {}

    def add(self, channel: Channel) -> None:
        """Stage a channel row."""
        self._rows[channel.id] = channel

    def get(self, tenant_id: UUID, channel_id: UUID) -> Channel | None:
        """Return the row only when the tenant owns it, else None."""
        # why: indistinguishable None keeps the API from confirming
        # cross-tenant existence (Constitution V).
        row = self._rows.get(channel_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row


class _QuarantineStore:
    """Minimal ``QuarantineRepository`` shape for the tenant proof."""

    def __init__(self) -> None:
        self._rows: dict[UUID, QuarantineRecord] = {}

    def add(self, record: QuarantineRecord) -> None:
        """Stage a quarantine row."""
        self._rows[record.id] = record

    def get(self, tenant_id: UUID, quarantine_id: UUID) -> QuarantineRecord | None:
        """Return the row only when the tenant owns it, else None."""
        row = self._rows.get(quarantine_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row


class _ImportJobStore:
    """Transient-job ledger mirroring the tenant-first port contract."""

    def __init__(self) -> None:
        self._rows: dict[UUID, ImportJob] = {}

    def add(self, job: ImportJob) -> None:
        """Stage a job row."""
        self._rows[job.id] = job

    def get(self, tenant_id: UUID, job_id: UUID) -> ImportJob | None:
        """Return the job only when the tenant owns it, else None."""
        row = self._rows.get(job_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row


def test_channel_store__cross_tenant__returns_none_without_leak() -> None:
    """Another tenant's channel id resolves to None, never to the row."""
    # // given ACME's channel in the store // when OTHER reads it
    # // then None; ACME still sees its own row.
    store = _ChannelStore()
    channel = Channel.create(
        tenant_id=ACME_TENANT,
        key="import-main",
        kind=ChannelKind.IMPORT,
        policy=ValidationPolicy(required_fields=("title",)),
        created_by=ADMIN_USER,
        now=FIXED_NOW,
    )
    store.add(channel)
    assert store.get(OTHER_TENANT, channel.id) is None
    assert store.get(ACME_TENANT, channel.id) == channel


def test_quarantine_store__cross_tenant__returns_none_without_leak() -> None:
    """Another tenant's quarantine id resolves to None, never to the row."""
    # // given ACME's HELD row in the store // when OTHER reads it
    # // then None; ACME still sees its own row.
    store = _QuarantineStore()
    record = _held_record()
    store.add(record)
    assert store.get(OTHER_TENANT, record.id) is None
    assert store.get(ACME_TENANT, record.id) == record


def test_import_job_store__cross_tenant__returns_none_without_leak() -> None:
    """Another tenant's job id resolves to None; the owner keeps access."""
    # // given ACME's previewed job in the ledger // when OTHER reads it
    # // then None; the job still carries ACME's tenant for downstream writes.
    store = _ImportJobStore()
    job = _previewed_job()
    store.add(job)
    assert store.get(OTHER_TENANT, job.id) is None
    owned = store.get(ACME_TENANT, job.id)
    assert owned is not None
    assert owned.tenant_id == ACME_TENANT
    assert owned.status is ImportJobStatus.PREVIEWED


def test_import_job__tenant_carried__resolves_with_actor() -> None:
    """The job pins its tenant so commits inherit it (no silent cross-write)."""
    # // given a job created by ACME's admin // when inspected
    # // then the tenant matches the actor's tenant, not the other tenant.
    job = _make_job()
    assert job.tenant_id == ACME_TENANT
    assert job.tenant_id != OTHER_TENANT
    assert job.created_by == ADMIN_USER
    assert job.created_by != OTHER_USER
