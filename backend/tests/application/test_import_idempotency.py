"""Tests for source-record id idempotency in the import pipeline (FM-026).

Covers the T1 acceptance criteria from `specs/002-leftovers/spec.md`:

1. **Dry-run writes zero case rows** — preview (even with the read-only
   ``case_repo`` supplied for already-imported duplicate detection)
   stages no case, audit, or outbox row.
2. **Batch duplicates reported** — two rows carrying the same
   source-record id in one package preview as 1 accepted + 1 rejected
   with ``duplicate_source_id``.
3. **Re-submission is idempotent** — committing the same package twice
   creates cases only on the first commit; the second commit reports
   duplicates with the original ``case_id`` attached and creates zero
   new rows.

Naming follows `test_<unit>_<scenario>__<expectation>` per AGENTS.md
§4.R5. Tests are deterministic: fixed clock, fixed UUIDs, in-memory
fakes (no container).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.application.imports.run_import import (
    CommitImportCommand,
    ImportRow,
    PreviewImportCommand,
)
from app.application.imports.run_import import (
    commit as import_commit,
)
from app.application.imports.run_import import (
    preview as import_preview,
)
from app.application.imports.source_ids import ERROR_DUPLICATE_SOURCE_ID
from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.import_job import (
    FieldMapping,
    ImportJobStatus,
    ImportRowOutcome,
    MappingVersion,
)

from .cases._fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
    InMemoryOutbox,
    make_case_type,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
ADMIN_USER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _make_id_mapping(*, case_type_key: str = "ServiceRequest") -> MappingVersion:
    """Mapping whose ``request_id`` column is the stable source-record id."""
    return MappingVersion(
        version=1,
        case_type_key=case_type_key,
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


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def seeded_repos() -> tuple[
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
]:
    """Seed ACME with a published type declaring the id target field.

    // given a case type whose schema declares ``requestId`` — the
    // target the mapping's ``source_id_field`` round-trips through.
    """
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()
    case_type, version = make_case_type(
        tenant_id=ACME_TENANT,
        required_keys=("title", "summary", "requestId"),
    )
    case_type_repo.add(case_type)
    version_repo.add(version)
    return case_repo, case_type_repo, version_repo


def _row(*, index: int, **values: str) -> ImportRow:
    return ImportRow(index=index, values=values)


def _preview_cmd(rows: list[ImportRow]) -> PreviewImportCommand:
    return PreviewImportCommand(
        tenant_id=ACME_TENANT,
        actor_id=ADMIN_USER,
        actor_roles=("process_admin",),
        source_label="synthetic-fixture",
        source_format="csv",
        mapping=_make_id_mapping(),
        rows=tuple(rows),
        correlation_id="corr-import-idem-001",
    )


def _commit_cmd(rows: list[ImportRow]) -> CommitImportCommand:
    return CommitImportCommand(
        tenant_id=ACME_TENANT,
        actor_id=ADMIN_USER,
        actor_roles=("process_admin",),
        source_label="synthetic-fixture",
        source_format="csv",
        mapping=_make_id_mapping(),
        rows=tuple(rows),
        correlation_id="corr-import-idem-001",
    )


def _valid_row(*, index: int, request_id: str, title: str = "T") -> ImportRow:
    """Build a row that passes validation for the seeded schema."""
    return _row(
        index=index,
        request_id=request_id,
        title=f"{title}-{index}",
        summary=f"Summary-{index}",
        priority="normal",
    )


# ---------------------------------------------------------------------------
# 1. Batch-internal duplicates in the dry-run preview
# ---------------------------------------------------------------------------


async def test_import_preview__repeated_source_id__rejects_repeat_as_duplicate(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
) -> None:
    """// given rows 1+3 share SR-0001 // when previewed // then row 3 is
    REJECTED/duplicate_source_id, rows 1+2 ACCEPTED, ids attached."""
    case_repo, case_type_repo, version_repo = seeded_repos
    rows = [
        _valid_row(index=1, request_id="SR-0001"),
        _valid_row(index=2, request_id="SR-0002"),
        _valid_row(index=3, request_id="SR-0001"),
    ]

    job = await import_preview(
        _preview_cmd(rows),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        case_repo=case_repo,
    )

    assert job.status is ImportJobStatus.PREVIEWED
    assert job.reconciliation is not None
    assert job.reconciliation.committed_count == 2
    assert job.reconciliation.rejected_count == 1
    by_index = {r.row_index: r for r in job.results}
    assert by_index[1].outcome is ImportRowOutcome.ACCEPTED
    assert by_index[2].outcome is ImportRowOutcome.ACCEPTED
    assert by_index[3].outcome is ImportRowOutcome.REJECTED
    assert by_index[3].error_code == ERROR_DUPLICATE_SOURCE_ID
    # Every result carries its source id for operator correlation.
    assert by_index[1].source_id == "SR-0001"
    assert by_index[2].source_id == "SR-0002"
    assert by_index[3].source_id == "SR-0001"


async def test_import_preview__already_rejected_row__keeps_root_cause(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
) -> None:
    """// given a repeat row that is also invalid // when previewed
    // then the original validation error wins over the duplicate code."""
    case_repo, case_type_repo, version_repo = seeded_repos
    rows = [
        _valid_row(index=1, request_id="SR-0001"),
        _row(
            index=2,
            request_id="SR-0001",
            title="",
            summary="Invalid: empty title.",
            priority="yesterday",
        ),
    ]

    job = await import_preview(
        _preview_cmd(rows),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        case_repo=case_repo,
    )

    by_index = {r.row_index: r for r in job.results}
    assert by_index[1].outcome is ImportRowOutcome.ACCEPTED
    assert by_index[2].outcome is ImportRowOutcome.REJECTED
    assert by_index[2].error_code != ERROR_DUPLICATE_SOURCE_ID


# ---------------------------------------------------------------------------
# 2. Dry-run writes zero rows
# ---------------------------------------------------------------------------


async def test_import_preview__with_read_repo__writes_zero_case_rows(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
) -> None:
    """Acceptance: dry-run writes zero case rows.

    // given valid rows and the read-only case_repo supplied // when
    // previewed // then no case, audit, or outbox row exists.
    """
    case_repo, case_type_repo, version_repo = seeded_repos
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    rows = [
        _valid_row(index=1, request_id="SR-0001"),
        _valid_row(index=2, request_id="SR-0002"),
    ]

    job = await import_preview(
        _preview_cmd(rows),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        case_repo=case_repo,
    )

    assert job.status is ImportJobStatus.PREVIEWED
    assert case_repo._cases == []
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 3. Idempotent re-submission on commit
# ---------------------------------------------------------------------------


async def test_import_commit__resubmitted_package__creates_no_duplicate_cases(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
    clock: FixedClock,
) -> None:
    """Acceptance: re-submitting same source-record id is idempotent.

    // given a committed 2-row package // when the same package commits
    // again // then zero new cases exist and both rows report
    // duplicate_source_id pointing at the original case ids.
    """
    case_repo, case_type_repo, version_repo = seeded_repos
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    rows = [
        _valid_row(index=1, request_id="SR-0001"),
        _valid_row(index=2, request_id="SR-0002"),
    ]

    # // when first commit.
    first = await import_commit(
        _commit_cmd(rows),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_NoopDocumentRepository(),
        document_store=_NoopDocumentStore(),
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert first.job.status is ImportJobStatus.COMMITTED
    assert len(case_repo._cases) == 2
    original_ids = {r.source_id: r.case_id for r in first.job.results}

    # // when same package re-submitted.
    second = await import_commit(
        _commit_cmd(rows),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_NoopDocumentRepository(),
        document_store=_NoopDocumentStore(),
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # // then no duplicate case: still exactly 2 rows.
    assert len(case_repo._cases) == 2
    assert second.job.reconciliation is not None
    assert second.job.reconciliation.committed_count == 0
    assert second.job.reconciliation.rejected_count == 2
    assert second.created_case_ids == ()
    for result in second.job.results:
        assert result.outcome is ImportRowOutcome.REJECTED
        assert result.error_code == ERROR_DUPLICATE_SOURCE_ID
        # The report correlates each duplicate to its original case.
        assert result.case_id == original_ids[result.source_id]


async def test_import_commit__mixed_new_and_known__creates_only_new(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
    clock: FixedClock,
) -> None:
    """// given SR-0001 already imported // when committing SR-0001 +
    SR-0003 // then exactly one new case lands and SR-0001 reports
    duplicate with the original case id."""
    case_repo, case_type_repo, version_repo = seeded_repos
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    first = await import_commit(
        _commit_cmd([_valid_row(index=1, request_id="SR-0001")]),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_NoopDocumentRepository(),
        document_store=_NoopDocumentStore(),
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    original_id = first.created_case_ids[0]

    second = await import_commit(
        _commit_cmd(
            [
                _valid_row(index=1, request_id="SR-0001"),
                _valid_row(index=2, request_id="SR-0003"),
            ]
        ),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_NoopDocumentRepository(),
        document_store=_NoopDocumentStore(),
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert len(case_repo._cases) == 2
    assert second.job.reconciliation is not None
    assert second.job.reconciliation.committed_count == 1
    assert second.job.reconciliation.rejected_count == 1
    by_index = {r.row_index: r for r in second.job.results}
    assert by_index[1].outcome is ImportRowOutcome.REJECTED
    assert by_index[1].error_code == ERROR_DUPLICATE_SOURCE_ID
    assert by_index[1].case_id == original_id
    assert by_index[2].outcome is ImportRowOutcome.ACCEPTED
    assert len(second.created_case_ids) == 1


async def test_import_preview__known_source_id__reports_duplicate_without_write(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
    clock: FixedClock,
) -> None:
    """// given SR-0001 committed // when previewed again with the read
    repo // then the row previews as duplicate and still writes nothing."""
    case_repo, case_type_repo, version_repo = seeded_repos
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    await import_commit(
        _commit_cmd([_valid_row(index=1, request_id="SR-0001")]),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_NoopDocumentRepository(),
        document_store=_NoopDocumentStore(),
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    audit_count = len(audit.events)
    outbox_count = len(outbox.events)

    job = await import_preview(
        _preview_cmd([_valid_row(index=1, request_id="SR-0001")]),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        case_repo=case_repo,
    )

    assert len(case_repo._cases) == 1
    assert len(audit.events) == audit_count
    assert len(outbox.events) == outbox_count
    (only,) = job.results
    assert only.outcome is ImportRowOutcome.REJECTED
    assert only.error_code == ERROR_DUPLICATE_SOURCE_ID


# ---------------------------------------------------------------------------
# 4. Misconfiguration fails closed
# ---------------------------------------------------------------------------


async def test_import_preview__source_id_target_not_in_schema__refuses_job(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
) -> None:
    """// given source_id_field maps to a target the type does not declare
    // when previewed // then the whole job is refused (fail-closed, so
    // a later re-submission could never be recognised)."""
    cmd = PreviewImportCommand(
        tenant_id=ACME_TENANT,
        actor_id=ADMIN_USER,
        actor_roles=("process_admin",),
        source_label="synthetic-fixture",
        source_format="csv",
        mapping=_make_id_mapping(),
        rows=(_valid_row(index=1, request_id="SR-0001"),),
        correlation_id="corr-import-idem-002",
    )
    # Swap in a case type that does not declare the id target field.
    other_type_repo = InMemoryCaseTypeRepository()
    other_version_repo = InMemoryCaseTypeVersionRepository()
    case_type, version = make_case_type(
        tenant_id=ACME_TENANT,
        required_keys=("title", "summary"),
    )
    other_type_repo.add(case_type)
    other_version_repo.add(version)

    with pytest.raises(ValidationFailed, match="source_id_field"):
        await import_preview(
            cmd,
            case_type_repo=other_type_repo,
            case_type_version_repo=other_version_repo,
        )


def test_mapping_version__source_id_field_outside_field_map__rejected() -> None:
    """// given source_id_field naming an unmapped column // when built
    // then construction raises (same rule as title/description/priority
    // pointers)."""
    with pytest.raises(InvariantViolation, match="source_id_field"):
        MappingVersion(
            version=1,
            case_type_key="ServiceRequest",
            fields=(FieldMapping(source="title", target="title"),),
            title_field="title",
            source_id_field="request_id",
        )


# ---------------------------------------------------------------------------
# Local fakes for document store + repository (commit path only)
# ---------------------------------------------------------------------------


class _NoopDocumentStore:
    """Stub ``DocumentStore`` — the idempotency slice never attaches docs."""

    async def put(self, **_kwargs: Any) -> str:  # pragma: no cover
        raise AssertionError("DocumentStore.put should not be called by this test")

    async def get(self, **_kwargs: Any) -> bytes:  # pragma: no cover
        raise AssertionError("DocumentStore.get should not be called by this test")


class _NoopDocumentRepository:
    """Stub ``DocumentRepository`` for the same reason as above."""

    async def add_document_and_version(self, **_kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("DocumentRepository.add_document_and_version should not be called")

    async def get(self, **_kwargs: Any) -> Any:  # pragma: no cover
        return None

    async def add_version(self, **_kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("DocumentRepository.add_version should not be called")

    async def list_versions(self, **_kwargs: Any) -> list[Any]:  # pragma: no cover
        return []


__all__: list[str] = []
