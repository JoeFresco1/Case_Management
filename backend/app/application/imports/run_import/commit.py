"""Commit use case: create cases and documents with audit + outbox (P14-US3 / FM-026)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.imports.http_dependencies import (
    ImportMappingRepository,
)
from app.application.imports.run_import.cases import _create_case_for_row
from app.application.imports.run_import.commands import (
    CommitImportCommand,
    CommitReport,
)
from app.application.imports.run_import.helpers import (
    ERROR_DOCUMENT_STORAGE_FAILED,
    IMPORT_COMMITTED,
    SUBJECT_TYPE_IMPORT_JOB,
    _code_for_exception,
    _reconciliation_from,
    _user_context,
)
from app.application.imports.run_import.policies import (
    _apply_source_id_policy,
    _resolve_mapping,
)
from app.application.imports.run_import.rows import _evaluate_row
from app.domain.errors import (
    DependencyUnavailable,
    InvariantViolation,
    NotFound,
    ValidationFailed,
)
from app.domain.import_job import (
    ImportJob,
    ImportRowOutcome,
    ImportRowResult,
)
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    CaseTypeRepository,
    CaseTypeVersionRepository,
    DocumentRepository,
    DocumentStore,
    OutboxWriter,
)

# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def commit(
    cmd: CommitImportCommand,
    *,
    case_repo: CaseRepository,
    case_type_repo: CaseTypeRepository,
    case_type_version_repo: CaseTypeVersionRepository,
    document_repo: DocumentRepository,
    document_store: DocumentStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
    import_mapping_repo: ImportMappingRepository | None = None,
) -> CommitReport:
    """Commit an import package — creates cases (and optional documents).

    The commit flow re-runs validation so a row that was acceptable at
    preview time but failed at commit time (race / storage failure) is
    quarantined, not silently dropped. The reconciliation on the
    resulting job reflects the actual commit outcome.
    """
    # 1. Authorization.
    await authorize(
        Action.ADMIN_TENANT_CONFIG,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="import_job"),
    )

    # 2. Resolve the mapping: either from the registry (mapping_id) or
    # inline (mapping). Only published mappings may back new jobs.
    mapping = await _resolve_mapping(cmd, import_mapping_repo)

    # 3. Resolve target case-type version (same as preview).
    case_type = await case_type_repo.get_by_key(cmd.tenant_id, mapping.case_type_key)
    if case_type is None:
        raise NotFound(f"case type {mapping.case_type_key!r} not found")
    if case_type.current_version_id is None:
        raise NotFound(f"case type {mapping.case_type_key!r} has no published version")
    version = await case_type_version_repo.get(cmd.tenant_id, case_type.current_version_id)
    if version is None:
        raise NotFound(f"published case type version {case_type.current_version_id} not found")

    # 4. Move into COMMITTING. The job is constructed in-memory for the
    # commit; persistence is the HTTP layer's responsibility (the
    # application layer owns the aggregate, not the row).
    now = clock.now()
    job = ImportJob.create(
        tenant_id=cmd.tenant_id,
        source_label=cmd.source_label,
        source_format=cmd.source_format,
        mapping=mapping,
        created_by=cmd.actor_id,
        now=now,
        correlation_id=cmd.correlation_id,
    )
    # Re-run validation deterministically against (row, mapping,
    # version), then apply the source-record id policy: batch-internal
    # repeats and already-imported ids are downgraded to REJECTED /
    # duplicate_source_id so the commit creates no duplicate case.
    evaluated = tuple(_evaluate_row(row, mapping, version) for row in cmd.rows)
    commit_results = await _apply_source_id_policy(
        evaluated,
        cmd.rows,
        mapping,
        version,
        tenant_id=cmd.tenant_id,
        case_repo=case_repo,
    )
    job = job.attach_preview(
        source_row_count=len(cmd.rows),
        results=commit_results,
        reconciliation=_reconciliation_from(commit_results),
        now=now,
    )
    job = job.mark_committing()

    # 4. Create one case per accepted row. Rejected rows are recorded
    # with the same outcome the preview produced; we do not re-evaluate
    # because the preview is deterministic against (row, mapping,
    # version) and we just re-ran it above.
    accepted_results = [r for r in job.results if r.outcome is ImportRowOutcome.ACCEPTED]
    accepted_rows_by_index = {r.row_index: r for r in accepted_results}
    # why: rows that pass preview validation but fail at commit time
    # (storage outage, config race) need a stable REJECTED entry with
    # a non-None error code — the preview's ACCEPTED entry carries no
    # code, so we track the failure explicitly instead of reusing it.
    commit_failures: dict[int, ImportRowResult] = {}

    created_case_ids: list[UUID] = []
    # We iterate the input rows so we can attach a document to the
    # matching case when the row carries one.
    for row in cmd.rows:
        result = accepted_rows_by_index.get(row.index)
        if result is None:
            continue
        try:
            case_id = await _create_case_for_row(
                row=row,
                mapping=mapping,
                version=version,
                case_repo=case_repo,
                document_repo=document_repo,
                document_store=document_store,
                audit=audit,
                outbox=outbox,
                actor_id=cmd.actor_id,
                tenant_id=cmd.tenant_id,
                correlation_id=cmd.correlation_id,
                now=now,
            )
        except DependencyUnavailable as exc:
            # Storage outage for this record only: quarantine the row
            # with a stable code and continue the batch. Nothing was
            # staged for this row (bytes are stored before any DB
            # write in ``_create_case_for_row``), so there is no
            # partial case to roll back. The outer ``db.commit()``
            # still rolls back everything if the shared transaction
            # itself fails.
            accepted_results.remove(result)
            accepted_rows_by_index.pop(row.index, None)
            commit_failures[row.index] = ImportRowResult(
                row_index=row.index,
                outcome=ImportRowOutcome.REJECTED,
                case_id=None,
                source_id=result.source_id,
                error_code=ERROR_DOCUMENT_STORAGE_FAILED,
                error_message=str(exc) or "document storage unavailable",
            )
            continue
        except (ValidationFailed, NotFound, InvariantViolation) as exc:
            # A row that passed validation at preview time but failed at
            # commit time is recorded as rejected — never silently
            # dropped. The rebuilt ``final_results`` tuple below carries
            # the new outcome with a stable error code.
            accepted_results.remove(result)
            accepted_rows_by_index.pop(row.index, None)
            commit_failures[row.index] = ImportRowResult(
                row_index=row.index,
                outcome=ImportRowOutcome.REJECTED,
                case_id=result.case_id,
                source_id=result.source_id,
                error_code=_code_for_exception(exc),
                error_message=str(exc),
            )
            continue
        created_case_ids.append(case_id)
        # Update the result with the assigned id. The source-record id
        # carrier is preserved so a later re-submission reconciles
        # against this exact row (T1 idempotency acceptance).
        accepted_rows_by_index[row.index] = ImportRowResult(
            row_index=row.index,
            outcome=ImportRowOutcome.ACCEPTED,
            case_id=case_id,
            source_id=result.source_id,
        )

    # 5. Rebuild results with the actual case ids and final
    # reconciliation. Both carriers (source_id for correlation,
    # case_id for duplicate-to-original navigation) are preserved —
    # dropping them would break idempotent re-submission reporting.
    # Commit-time failures come from ``commit_failures`` (stable code);
    # preview-time rejections keep their original code.
    final_results = tuple(
        accepted_rows_by_index.get(
            r.row_index,
            commit_failures.get(
                r.row_index,
                ImportRowResult(
                    row_index=r.row_index,
                    outcome=ImportRowOutcome.REJECTED,
                    case_id=r.case_id,
                    source_id=r.source_id,
                    error_code=r.error_code,
                    error_message=r.error_message,
                ),
            ),
        )
        for r in job.results
    )
    reconciliation = _reconciliation_from(final_results)
    committed_job = job.mark_committed(
        results=final_results,
        reconciliation=reconciliation,
        at=clock.now(),
    )

    # 6. Append the import-job audit + outbox events so the operator
    # console can list committed jobs by their own event type.
    committed_now = clock.now()
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=IMPORT_COMMITTED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_IMPORT_JOB,
        subject_id=committed_job.id,
        case_id=None,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "source_label": committed_job.source_label,
            "source_format": committed_job.source_format,
            "source_row_count": committed_job.source_row_count,
            "committed_count": reconciliation.committed_count,
            "rejected_count": reconciliation.rejected_count,
            "skipped_count": reconciliation.skipped_count,
        },
        occurred_at=committed_now,
    )
    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=IMPORT_COMMITTED,
        event_id=uuid4(),
        payload={
            "import_job_id": str(committed_job.id),
            "tenant_id": str(cmd.tenant_id),
            "committed_count": reconciliation.committed_count,
            "rejected_count": reconciliation.rejected_count,
            "occurred_at": committed_now.isoformat(),
        },
    )

    rejected_count = reconciliation.rejected_count
    return CommitReport(
        job=committed_job,
        created_case_ids=tuple(created_case_ids),
        rejected_count=rejected_count,
    )
