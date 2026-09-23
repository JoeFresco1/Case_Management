"""SQL composition provider for the import pipeline HTTP boundary.

This adapter stays outside ``app.interfaces``. It binds the local
SQLAlchemy repositories, the transaction-bound audit / outbox writers,
and the import-job recorder to the infra-free import provider
contract; an Azure adapter can implement the same contract later.

The import-job row translation (aggregate -> ``ImportJob`` ORM row)
lives here — not in the router — because the ORM row shape is a
persistence concern. The router works purely with the ``ImportJob``
aggregate and the verbatim mapping snapshot dict.

Read-side reconstruction (``get`` / ``list_for_tenant``) rebuilds the
``ImportJob`` aggregate from the stored JSON columns so the operator
UI can inspect a previously previewed or committed job without
re-computing the reconciliation (FM-027).
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.imports.http_dependencies import ImportHttpRepositories
from app.domain.import_job import (
    ImportJob,
    ImportJobStatus,
    ImportReconciliation,
    ImportRowOutcome,
    ImportRowResult,
    MappingVersion,
)
from app.infrastructure.persistence.audit_outbox import (
    SqlAuditEventStore,
    SqlOutboxWriter,
)
from app.infrastructure.persistence.documents import SqlDocumentRepository
from app.infrastructure.persistence.models import ImportJob as ImportJobRow
from app.infrastructure.persistence.repositories import (
    SqlCaseRepository,
    SqlCaseTypeRepository,
    SqlCaseTypeVersionRepository,
    SqlImportMappingRepository,
    SqlRoleRepository,
)


def _reconstruct_mapping(mapping_json: dict[str, Any]) -> MappingVersion:
    """Rebuild a ``MappingVersion`` from the persisted mapping JSON blob."""
    from app.domain.import_job import DocumentMapping, FieldMapping  # noqa: PLC0415

    documents_raw = mapping_json.get("documents")
    documents: DocumentMapping | None = None
    if documents_raw is not None:
        documents = DocumentMapping(
            file_name_field=documents_raw.get("file_name_field", ""),
            content_field=documents_raw.get("content_field", ""),
            content_type_field=documents_raw.get("content_type_field", ""),
            classification_field=documents_raw.get("classification_field", ""),
            sha256_field=documents_raw.get("sha256_field", ""),
        )
    return MappingVersion(
        version=mapping_json.get("version", 1),
        case_type_key=mapping_json.get("case_type_key", ""),
        fields=tuple(
            FieldMapping(source=f["source"], target=f["target"])
            for f in mapping_json.get("fields", [])
        ),
        title_field=mapping_json.get("title_field"),
        description_field=mapping_json.get("description_field"),
        priority_field=mapping_json.get("priority_field"),
        source_id_field=mapping_json.get("source_id_field"),
        documents=documents,
    )


def _reconstruct_job(row: ImportJobRow) -> ImportJob:
    """Rebuild an ``ImportJob`` aggregate from its ORM row.

    The per-row results and reconciliation are flattened into JSON
    columns at write time; this helper reverses that projection so the
    router can return the same shape the preview/commit produced.
    """
    mapping = _reconstruct_mapping(row.mapping_json or {})

    results: tuple[ImportRowResult, ...] = tuple(
        ImportRowResult(
            row_index=r["row_index"],
            outcome=ImportRowOutcome(r["outcome"]),
            case_id=UUID(r["case_id"]) if r.get("case_id") else None,
            source_id=r.get("source_id"),
            error_code=r.get("error_code"),
            error_message=r.get("error_message"),
        )
        for r in (row.results_json or [])
    )

    recon_json = row.reconciliation_json
    reconciliation: ImportReconciliation | None = None
    if recon_json is not None:
        reconciliation = ImportReconciliation(
            committed_count=recon_json.get("committed_count", 0),
            rejected_count=recon_json.get("rejected_count", 0),
            skipped_count=recon_json.get("skipped_count", 0),
        )

    return ImportJob(
        id=row.id,
        tenant_id=row.tenant_id,
        source_label=row.source_label,
        source_format=row.source_format,
        source_row_count=row.source_row_count,
        mapping=mapping,
        status=ImportJobStatus(row.status),
        created_at=row.created_at,
        created_by=row.created_by,
        correlation_id=row.correlation_id,
        results=results,
        reconciliation=reconciliation,
        committed_at=row.committed_at,
        failure_reason=row.failure_reason,
    )


class SqlImportJobRecorder:
    """Translate the ``ImportJob`` aggregate into its ORM row and stage it.

    The reconciliation is flattened into the row's JSON column so a
    follow-up read does not have to recompute it. The mapping snapshot
    is stored verbatim so an old import is replayed against the
    mapping it was committed with (Constitution XI).

    Also provides read-side reconstruction so the operator UI can
    inspect a previously previewed or committed job.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(self, job: ImportJob, mapping: dict[str, Any]) -> None:
        """Stage ``job`` with its ``mapping`` snapshot on the request transaction."""
        self._s.add(
            ImportJobRow(
                id=job.id,
                tenant_id=job.tenant_id,
                source_label=job.source_label,
                source_format=job.source_format,
                source_row_count=job.source_row_count,
                mapping_json=dict(mapping),
                status=job.status.value,
                correlation_id=job.correlation_id,
                created_by=job.created_by,
                created_at=job.created_at,
                updated_at=job.created_at,
                committed_at=job.committed_at,
                failure_reason=job.failure_reason,
                reconciliation_json=(
                    {
                        "committed_count": job.reconciliation.committed_count,
                        "rejected_count": job.reconciliation.rejected_count,
                        "skipped_count": job.reconciliation.skipped_count,
                    }
                    if job.reconciliation is not None
                    else None
                ),
                results_json=[
                    {
                        "row_index": r.row_index,
                        "outcome": r.outcome.value,
                        "case_id": str(r.case_id) if r.case_id else None,
                        "source_id": r.source_id,
                        "error_code": r.error_code,
                        "error_message": r.error_message,
                    }
                    for r in job.results
                ],
            )
        )

    async def get(self, tenant_id: UUID, job_id: UUID) -> ImportJob | None:
        """Return the job aggregate for ``job_id`` in ``tenant_id``."""
        row = (
            await self._s.execute(
                select(ImportJobRow).where(
                    ImportJobRow.tenant_id == tenant_id,
                    ImportJobRow.id == job_id,
                )
            )
        ).scalar_one_or_none()
        return _reconstruct_job(row) if row else None

    async def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ImportJob]:
        """List import jobs for ``tenant_id``, newest first."""
        rows = (
            (
                await self._s.execute(
                    select(ImportJobRow)
                    .where(ImportJobRow.tenant_id == tenant_id)
                    .order_by(ImportJobRow.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return [_reconstruct_job(r) for r in rows]


class SqlImportHttpDependencyProvider:
    """Bind all import HTTP ports to one SQLAlchemy session."""

    def for_session(self, session: object) -> ImportHttpRepositories:
        """Create SQL adapters sharing the supplied request transaction."""
        db = cast(AsyncSession, session)
        return ImportHttpRepositories(
            case=SqlCaseRepository(db),
            case_type=SqlCaseTypeRepository(db),
            case_type_version=SqlCaseTypeVersionRepository(db),
            document=SqlDocumentRepository(db),
            audit=SqlAuditEventStore(db),
            outbox=SqlOutboxWriter(db),
            role=SqlRoleRepository(db),
            import_jobs=SqlImportJobRecorder(db),
            import_mappings=SqlImportMappingRepository(db),
        )


__all__ = ["SqlImportHttpDependencyProvider", "SqlImportJobRecorder", "SqlImportMappingRepository"]
