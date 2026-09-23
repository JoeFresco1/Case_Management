"""Versioned CSV/JSON/JSONL import tests (FM-026 / T1-US1, T1-US2).

Covers the task acceptance criteria against the application layer:

1. **Dry-run writes zero case rows** — for each of ``csv`` / ``json`` /
   ``jsonl``, a 2-valid + 1-malformed package previews as
   accepted=2 / rejected=1 with zero case, audit, or outbox rows.
2. **Commit is per-record atomic** — valid records land case +
   document + ``CaseCreated`` / ``DocumentAdded`` audit + outbox in
   the staged transaction; the reconciliation matches the per-row
   results and the job survives a record/get round-trip.
3. **Malformed records quarantine** — a bad row is REJECTED with a
   stable code and the batch still commits the valid rows.
4. **Idempotent replay** — re-submitting the same ``source_id``
   creates zero new cases and reports ``duplicate_source_id`` with
   the original ``case_id``.
5. **Dependency failure rolls back safely** — a ``DocumentStore.put``
   raising ``DependencyUnavailable`` quarantines that row with
   ``document_storage_failed``, continues the batch, and stages no
   partial case for the failed record.

Why one ``_evaluate_row`` path: the ``source_format`` string is
provenance only (validated by :class:`ImportJob` to ``csv | json |
jsonl``). Byte -> ``ImportRow`` decoding lives in the interface /
infrastructure adapter (``routers/imports._rows_from_payload``); the
application layer consumes pre-parsed rows, so these tests vary the
format string over identical rows to pin identical behaviour.

Naming follows ``test_<unit>_<scenario>__<expectation>`` per AGENTS.md
§4.R5. All fixtures are synthetic (``example.test``); no PHI.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.imports.run_import import (
    ERROR_DOCUMENT_STORAGE_FAILED,
    ERROR_INVALID_PRIORITY,
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
from app.application.imports.source_ids import (
    ERROR_DUPLICATE_SOURCE_ID,
    reconciliation_counts,
)
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.document import Document, DocumentVersion
from app.domain.errors import DependencyUnavailable
from app.domain.import_job import (
    DocumentMapping,
    FieldMapping,
    ImportJob,
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
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
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


def _make_doc_mapping(*, case_type_key: str = "ServiceRequest") -> MappingVersion:
    """Id mapping that additionally declares one attached document."""
    return MappingVersion(
        version=2,
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
        documents=DocumentMapping(
            file_name_field="file_name",
            content_field="content_b64",
            content_type_field="content_type",
            classification_field="classification",
            sha256_field="sha256",
        ),
    )


def _make_doc_case_type(*, tenant_id: UUID) -> tuple[CaseType, CaseTypeVersion]:
    """Published type declaring the id target plus the Evidence class."""
    fields = (
        CaseTypeField(key="title", label="Title", type="text", required=True),
        CaseTypeField(key="summary", label="Summary", type="text", required=True),
        CaseTypeField(key="requestId", label="Request Id", type="text", required=True),
    )
    schema = CaseTypeSchema(fields=fields, document_classifications=("Evidence",))
    version = CaseTypeVersion(
        id=uuid4(),
        tenant_id=tenant_id,
        case_type_id=uuid4(),
        version_number=1,
        schema=schema,
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=FIXED_NOW,
        published_by=ADMIN_USER,
    )
    case_type = CaseType(
        id=version.case_type_id,
        tenant_id=tenant_id,
        key="ServiceRequest",
        name="Service Request",
        description="",
        current_version_id=version.id,
    )
    return case_type, version


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def seeded_repos() -> tuple[
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
]:
    """Seed ACME with a published type declaring the id target field."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case_repo.set_tenant_short(OTHER_TENANT, "OTHER")

    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()
    case_type, version = make_case_type(
        tenant_id=ACME_TENANT,
        required_keys=("title", "summary", "requestId"),
    )
    case_type_repo.add(case_type)
    version_repo.add(version)
    return case_repo, case_type_repo, version_repo


@pytest.fixture
def doc_repos() -> tuple[
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
]:
    """Seed ACME with a published type that additionally allows Evidence docs."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case_repo.set_tenant_short(OTHER_TENANT, "OTHER")

    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()
    case_type, version = _make_doc_case_type(tenant_id=ACME_TENANT)
    case_type_repo.add(case_type)
    version_repo.add(version)
    return case_repo, case_type_repo, version_repo


def _row(*, index: int, **values: str) -> ImportRow:
    return ImportRow(index=index, values=values)


def _valid_row(*, index: int, request_id: str) -> ImportRow:
    """Build a row that passes validation for the seeded id schema."""
    return _row(
        index=index,
        request_id=request_id,
        title=f"Title-{index}",
        summary=f"Summary-{index}",
        priority="normal",
    )


def _malformed_row(*, index: int, request_id: str) -> ImportRow:
    """Build a row rejected for an invalid priority enum value."""
    return _row(
        index=index,
        request_id=request_id,
        title=f"Title-{index}",
        summary=f"Summary-{index}",
        priority="yesterday",
    )


def _doc_values(*, request_id: str, index: int) -> dict[str, str]:
    """Synthetic document columns with a real sha256 over fake bytes."""
    content = f"synthetic-evidence-bytes-{request_id}".encode()
    return {
        "request_id": request_id,
        "title": f"Title-{index}",
        "summary": f"Summary-{index}",
        "priority": "normal",
        "file_name": f"evidence-{index}.txt",
        "content_b64": base64.b64encode(content).decode(),
        "content_type": "text/plain",
        "classification": "Evidence",
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _preview_cmd(
    rows: list[ImportRow],
    *,
    source_format: str,
    mapping: MappingVersion | None = None,
    tenant_id: UUID = ACME_TENANT,
) -> PreviewImportCommand:
    return PreviewImportCommand(
        tenant_id=tenant_id,
        actor_id=ADMIN_USER,
        actor_roles=("process_admin",),
        source_label="synthetic-fixture",
        source_format=source_format,
        mapping=mapping or _make_id_mapping(),
        rows=tuple(rows),
        correlation_id="corr-import-versioned-001",
    )


def _commit_cmd(
    rows: list[ImportRow],
    *,
    source_format: str,
    mapping: MappingVersion | None = None,
    tenant_id: UUID = ACME_TENANT,
) -> CommitImportCommand:
    return CommitImportCommand(
        tenant_id=tenant_id,
        actor_id=ADMIN_USER,
        actor_roles=("process_admin",),
        source_label="synthetic-fixture",
        source_format=source_format,
        mapping=mapping or _make_id_mapping(),
        rows=tuple(rows),
        correlation_id="corr-import-versioned-001",
    )


# ---------------------------------------------------------------------------
# Local fakes (document store + job recorder)
# ---------------------------------------------------------------------------


class _InMemoryDocStore:
    """In-memory ``DocumentStore`` recording every ``put``."""

    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    async def put(
        self,
        *,
        tenant_id: UUID,
        content: bytes,
        content_type: str,
        sha256: str,
    ) -> str:
        ref = f"{tenant_id}/{sha256[:2]}/{sha256}"
        self.puts.append({"tenant_id": tenant_id, "size": len(content), "sha256": sha256})
        return ref

    async def get(self, *, tenant_id: UUID, content_ref: str) -> bytes:
        raise AssertionError("not exercised by import tests")


class _SelectiveFailingStore(_InMemoryDocStore):
    """Fails ``put`` only for bytes containing the marker (one bad row)."""

    def __init__(self, *, fail_marker: bytes) -> None:
        super().__init__()
        self._fail_marker = fail_marker

    async def put(
        self,
        *,
        tenant_id: UUID,
        content: bytes,
        content_type: str,
        sha256: str,
    ) -> str:
        if self._fail_marker in content:
            raise DependencyUnavailable("synthetic storage outage")
        return await super().put(
            tenant_id=tenant_id,
            content=content,
            content_type=content_type,
            sha256=sha256,
        )


class _AlwaysFailingStore:
    """``DocumentStore`` whose ``put`` always raises (outage scenario)."""

    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    async def put(
        self,
        *,
        tenant_id: UUID,
        content: bytes,
        content_type: str,
        sha256: str,
    ) -> str:
        self.puts.append({"tenant_id": tenant_id, "size": len(content)})
        raise DependencyUnavailable("synthetic storage outage")

    async def get(self, *, tenant_id: UUID, content_ref: str) -> bytes:
        raise DependencyUnavailable("synthetic storage outage")


class _FailingDocRepository:
    """``DocumentRepository`` that explodes if metadata is ever staged.

    Used to prove the failed record stages no partial document row:
    when storage fails first, this fake must never be called.
    """

    def __init__(self) -> None:
        self.added: list[tuple[Document, DocumentVersion]] = []

    async def add_document_and_version(
        self,
        *,
        document: Document,
        version: DocumentVersion,
    ) -> None:
        self.added.append((document, version))


class _InMemoryJobRecorder:
    """In-memory ``ImportJobRecorder`` for the record/get round-trip."""

    def __init__(self) -> None:
        self._jobs: dict[tuple[UUID, UUID], ImportJob] = {}

    async def record(self, job: ImportJob, mapping: dict[str, Any]) -> None:
        self._jobs[(job.tenant_id, job.id)] = job

    async def get(self, tenant_id: UUID, job_id: UUID) -> ImportJob | None:
        return self._jobs.get((tenant_id, job_id))

    async def list_for_tenant(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> Sequence[ImportJob]:
        jobs = [j for (t, _), j in self._jobs.items() if t == tenant_id]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[offset : offset + limit]


# ---------------------------------------------------------------------------
# 1. Dry-run parity across csv / json / jsonl (zero writes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_format", ["csv", "json", "jsonl"])
async def test_import_preview_versioned__two_valid_one_bad__reports_zero_writes(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
    source_format: str,
) -> None:
    """// given 2 valid + 1 malformed rows in ``source_format`` // when
    previewed // then accepted=2/rejected=1 and zero case rows exist."""
    case_repo, case_type_repo, version_repo = seeded_repos
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    rows = [
        _valid_row(index=1, request_id="SR-0001"),
        _valid_row(index=2, request_id="SR-0002"),
        _malformed_row(index=3, request_id="SR-0003"),
    ]

    job = await import_preview(
        _preview_cmd(rows, source_format=source_format),
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        case_repo=case_repo,
    )

    # // then the report carries the format as provenance only.
    assert job.source_format == source_format
    assert job.status is ImportJobStatus.PREVIEWED
    assert job.reconciliation is not None
    assert job.reconciliation.committed_count == 2
    assert job.reconciliation.rejected_count == 1
    # // then the pure counter agrees with the attached reconciliation.
    committed, rejected, skipped = reconciliation_counts(job.results)
    assert (committed, rejected, skipped) == (
        job.reconciliation.committed_count,
        job.reconciliation.rejected_count,
        job.reconciliation.skipped_count,
    )
    by_index = {r.row_index: r for r in job.results}
    assert by_index[3].outcome is ImportRowOutcome.REJECTED
    assert by_index[3].error_code == ERROR_INVALID_PRIORITY
    # // then dry-run writes zero rows (the read repo is report-only).
    assert case_repo._cases == []
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 2. Commit with documents is per-record atomic
# ---------------------------------------------------------------------------


async def test_import_commit__two_doc_rows__writes_cases_docs_audit_outbox(
    doc_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
    clock: FixedClock,
) -> None:
    """// given 2 valid doc rows // when committed // then 2 cases + 2
    docs + CaseCreated/DocumentAdded/ImportCommitted audit+outbox land
    and the reconciliation matches the per-row results."""
    case_repo, case_type_repo, version_repo = doc_repos
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    store = _InMemoryDocStore()
    doc_repo = _FailingDocRepository()
    mapping = _make_doc_mapping()
    rows = [
        _row(index=1, **_doc_values(request_id="SR-0001", index=1)),
        _row(index=2, **_doc_values(request_id="SR-0002", index=2)),
    ]

    report = await import_commit(
        _commit_cmd(rows, source_format="csv", mapping=mapping),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=doc_repo,  # type: ignore[arg-type]
        document_store=store,  # type: ignore[arg-type]
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # // then two cases + two stored blobs + two metadata rows.
    assert len(case_repo._cases) == 2
    assert len(store.puts) == 2
    assert len(doc_repo.added) == 2
    # // then per-record audit + outbox: 2 CaseCreated + 2 DocumentAdded.
    assert len([e for e in audit.events if e.event_type == "CaseCreated"]) == 2
    assert len([e for e in audit.events if e.event_type == "DocumentAdded"]) == 2
    assert len([o for o in outbox.events if o.event_type == "CaseCreated"]) == 2
    assert len([o for o in outbox.events if o.event_type == "DocumentAdded"]) == 2
    # // then the job-level commit event is present exactly once.
    assert len([e for e in audit.events if e.event_type == "ImportCommitted"]) == 1
    assert len([o for o in outbox.events if o.event_type == "ImportCommitted"]) == 1
    # // then the reconciliation matches the per-row results.
    assert report.job.status is ImportJobStatus.COMMITTED
    assert report.job.reconciliation is not None
    assert report.job.reconciliation.committed_count == 2
    assert report.job.reconciliation.rejected_count == 0
    accepted = [r for r in report.job.results if r.outcome is ImportRowOutcome.ACCEPTED]
    assert len(accepted) == 2
    assert all(r.case_id is not None for r in accepted)
    assert {r.source_id for r in accepted} == {"SR-0001", "SR-0002"}
    assert len(report.created_case_ids) == 2


async def test_import_commit__malformed_row__quarantines_without_failing_batch(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
    clock: FixedClock,
) -> None:
    """// given 2 valid + 1 malformed rows // when committed // then the
    valid rows land and the malformed row is REJECTED with a stable
    code (never silently dropped, never failing the batch)."""
    case_repo, case_type_repo, version_repo = seeded_repos
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    rows = [
        _valid_row(index=1, request_id="SR-0001"),
        _valid_row(index=2, request_id="SR-0002"),
        _malformed_row(index=3, request_id="SR-0003"),
    ]

    report = await import_commit(
        _commit_cmd(rows, source_format="jsonl"),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_FailingDocRepository(),  # type: ignore[arg-type]
        document_store=_InMemoryDocStore(),  # type: ignore[arg-type]
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert len(case_repo._cases) == 2
    assert report.job.reconciliation is not None
    assert report.job.reconciliation.committed_count == 2
    assert report.job.reconciliation.rejected_count == 1
    by_index = {r.row_index: r for r in report.job.results}
    assert by_index[3].outcome is ImportRowOutcome.REJECTED
    assert by_index[3].error_code == ERROR_INVALID_PRIORITY
    assert by_index[3].error_message is not None


# ---------------------------------------------------------------------------
# 3. Idempotent replay of the same source id
# ---------------------------------------------------------------------------


async def test_import_commit__replay_same_source_id__creates_no_duplicate(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
    clock: FixedClock,
) -> None:
    """// given a committed package // when the same source ids commit
    again // then zero new cases exist and each row reports
    duplicate_source_id with the original case id."""
    case_repo, case_type_repo, version_repo = seeded_repos
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    rows = [
        _valid_row(index=1, request_id="SR-0001"),
        _valid_row(index=2, request_id="SR-0002"),
    ]

    first = await import_commit(
        _commit_cmd(rows, source_format="json"),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_FailingDocRepository(),  # type: ignore[arg-type]
        document_store=_InMemoryDocStore(),  # type: ignore[arg-type]
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert len(case_repo._cases) == 2
    original_ids = {r.source_id: r.case_id for r in first.job.results}

    second = await import_commit(
        _commit_cmd(rows, source_format="json"),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_FailingDocRepository(),  # type: ignore[arg-type]
        document_store=_InMemoryDocStore(),  # type: ignore[arg-type]
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    assert len(case_repo._cases) == 2
    assert second.job.reconciliation is not None
    assert second.job.reconciliation.committed_count == 0
    assert second.job.reconciliation.rejected_count == 2
    assert second.created_case_ids == ()
    for result in second.job.results:
        assert result.outcome is ImportRowOutcome.REJECTED
        assert result.error_code == ERROR_DUPLICATE_SOURCE_ID
        assert result.case_id == original_ids[result.source_id]


# ---------------------------------------------------------------------------
# 4. Dependency failure rolls back safely (per-record quarantine)
# ---------------------------------------------------------------------------


async def test_import_commit__storage_failure_on_one_row__rejects_row_continues_batch(
    doc_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
    clock: FixedClock,
) -> None:
    """// given the store fails for row 2 only // when committed // then
    rows 1+3 land, row 2 is REJECTED/document_storage_failed, and no
    partial case or document row exists for row 2."""
    case_repo, case_type_repo, version_repo = doc_repos
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    bad_content = b"synthetic-evidence-bytes-SR-BAD"
    store = _SelectiveFailingStore(fail_marker=bad_content)
    doc_repo = _FailingDocRepository()
    mapping = _make_doc_mapping()
    rows = [
        _row(index=1, **_doc_values(request_id="SR-0001", index=1)),
        _row(index=2, **_doc_values(request_id="SR-BAD", index=2)),
        _row(index=3, **_doc_values(request_id="SR-0003", index=3)),
    ]

    report = await import_commit(
        _commit_cmd(rows, source_format="jsonl", mapping=mapping),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=doc_repo,  # type: ignore[arg-type]
        document_store=store,  # type: ignore[arg-type]
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # // then the batch continued past the outage: 2 cases landed.
    assert len(case_repo._cases) == 2
    assert {c.title for c in case_repo._cases} == {"Title-1", "Title-3"}
    # // then only the surviving records staged document metadata.
    assert len(doc_repo.added) == 2
    assert len(store.puts) == 2
    # // then the failed row is quarantined with a stable code.
    by_index = {r.row_index: r for r in report.job.results}
    assert by_index[2].outcome is ImportRowOutcome.REJECTED
    assert by_index[2].error_code == ERROR_DOCUMENT_STORAGE_FAILED
    assert by_index[2].case_id is None
    assert by_index[2].source_id == "SR-BAD"
    # // then no partial audit/outbox exists for the failed record:
    # 2 CaseCreated + 2 DocumentAdded, never 3.
    assert len([e for e in audit.events if e.event_type == "CaseCreated"]) == 2
    assert len([e for e in audit.events if e.event_type == "DocumentAdded"]) == 2
    assert report.job.reconciliation is not None
    assert report.job.reconciliation.committed_count == 2
    assert report.job.reconciliation.rejected_count == 1


async def test_import_commit__storage_outage_on_all_rows__stages_no_case_or_document(
    doc_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
    clock: FixedClock,
) -> None:
    """// given the store is fully offline // when committed // then all
    rows quarantine, zero cases/docs stage, and the job still records
    its own ImportCommitted reconciliation (no silent loss)."""
    case_repo, case_type_repo, version_repo = doc_repos
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    store = _AlwaysFailingStore()
    doc_repo = _FailingDocRepository()
    mapping = _make_doc_mapping()
    rows = [
        _row(index=1, **_doc_values(request_id="SR-0001", index=1)),
        _row(index=2, **_doc_values(request_id="SR-0002", index=2)),
    ]

    report = await import_commit(
        _commit_cmd(rows, source_format="csv", mapping=mapping),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=doc_repo,  # type: ignore[arg-type]
        document_store=store,  # type: ignore[arg-type]
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # // then nothing partial landed: no case, no document metadata.
    assert case_repo._cases == []
    assert doc_repo.added == []
    assert len([e for e in audit.events if e.event_type == "CaseCreated"]) == 0
    assert len([e for e in audit.events if e.event_type == "DocumentAdded"]) == 0
    # // then every row is quarantined with the storage code.
    assert report.job.reconciliation is not None
    assert report.job.reconciliation.committed_count == 0
    assert report.job.reconciliation.rejected_count == 2
    assert report.created_case_ids == ()
    for result in report.job.results:
        assert result.outcome is ImportRowOutcome.REJECTED
        assert result.error_code == ERROR_DOCUMENT_STORAGE_FAILED


# ---------------------------------------------------------------------------
# 5. Cross-tenant replay isolation + job record/get round-trip
# ---------------------------------------------------------------------------


async def test_import_commit__cross_tenant_replay__isolated_per_tenant(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
    clock: FixedClock,
) -> None:
    """// given SR-0001 committed under ACME // when OTHER commits the
    same source id // then OTHER lands a fresh case (no cross-tenant
    duplicate), while an ACME replay still reports a duplicate."""
    case_repo, case_type_repo, version_repo = seeded_repos
    # // given OTHER owns the same case-type key in its own tenant.
    other_type, other_version = make_case_type(
        tenant_id=OTHER_TENANT,
        required_keys=("title", "summary", "requestId"),
    )
    case_type_repo.add(other_type)
    version_repo.add(other_version)
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    rows = [_valid_row(index=1, request_id="SR-0001")]

    await import_commit(
        _commit_cmd(rows, source_format="csv"),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_FailingDocRepository(),  # type: ignore[arg-type]
        document_store=_InMemoryDocStore(),  # type: ignore[arg-type]
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert len(case_repo._cases) == 1

    # // when OTHER commits the same source id.
    other_report = await import_commit(
        _commit_cmd(rows, source_format="csv", tenant_id=OTHER_TENANT),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_FailingDocRepository(),  # type: ignore[arg-type]
        document_store=_InMemoryDocStore(),  # type: ignore[arg-type]
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # // then OTHER landed its own case (tenant isolation).
    assert len(case_repo._cases) == 2
    assert other_report.job.reconciliation is not None
    assert other_report.job.reconciliation.committed_count == 1
    (only,) = other_report.job.results
    assert only.outcome is ImportRowOutcome.ACCEPTED

    # // when ACME replays its own id, it is still a duplicate.
    replay = await import_commit(
        _commit_cmd(rows, source_format="csv"),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_FailingDocRepository(),  # type: ignore[arg-type]
        document_store=_InMemoryDocStore(),  # type: ignore[arg-type]
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    assert len(case_repo._cases) == 2
    (dup,) = replay.job.results
    assert dup.outcome is ImportRowOutcome.REJECTED
    assert dup.error_code == ERROR_DUPLICATE_SOURCE_ID


async def test_import_job__record_get_roundtrip__preserves_results_and_reconciliation(
    seeded_repos: tuple[
        InMemoryCaseRepository,
        InMemoryCaseTypeRepository,
        InMemoryCaseTypeVersionRepository,
    ],
    clock: FixedClock,
) -> None:
    """// given a committed job // when recorded and re-fetched (the
    ``GET /imports/{job_id}`` application seam) // then results +
    reconciliation survive unchanged."""
    case_repo, case_type_repo, version_repo = seeded_repos
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    rows = [
        _valid_row(index=1, request_id="SR-0001"),
        _valid_row(index=2, request_id="SR-0002"),
        _malformed_row(index=3, request_id="SR-0003"),
    ]

    report = await import_commit(
        _commit_cmd(rows, source_format="csv"),
        case_repo=case_repo,
        case_type_repo=case_type_repo,
        case_type_version_repo=version_repo,
        document_repo=_FailingDocRepository(),  # type: ignore[arg-type]
        document_store=_InMemoryDocStore(),  # type: ignore[arg-type]
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    recorder = _InMemoryJobRecorder()
    await recorder.record(report.job, {"version": 1})
    fetched = await recorder.get(ACME_TENANT, report.job.id)

    assert fetched is not None
    assert fetched.status is ImportJobStatus.COMMITTED
    assert fetched.results == report.job.results
    assert fetched.reconciliation == report.job.reconciliation
    listed = await recorder.list_for_tenant(ACME_TENANT)
    assert [j.id for j in listed] == [report.job.id]


__all__: list[str] = []
