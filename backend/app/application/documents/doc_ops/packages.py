"""Packages, hash-dedup, and CSV export use cases (T5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.doc_ops import (
    CsvRow,
    DedupCandidate,
    DocPackage,
    PackageMember,
    build_csv_export,
    find_duplicate_groups,
)
from app.domain.ports import AuditEventStore, DocumentRepository, OutboxWriter

from ._common import (
    CSV_EXPORTED,
    DOC_PACKAGE_BUILT,
    SUBJECT_TYPE_DOCUMENT,
    append_audit_and_outbox,
    has_action,
    proxy,
    resolve_document,
)


@dataclass(frozen=True, slots=True)
class BuildPackageCommand:
    """Input to :func:`build_package`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    name: str
    member_document_ids: tuple[UUID, ...]


async def build_package(
    cmd: BuildPackageCommand,
    *,
    document_repo: DocumentRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> DocPackage:
    """Assemble a manifest-bearing multi-document export envelope.

    Every member is resolved in-tenant (``NotFound`` otherwise) and
    requires ``DOCUMENT_READ``; a ``restricted`` / ``phi`` member
    additionally requires ``DOCUMENT_READ_RESTRICTED`` (fail-closed —
    bundle membership or a plain read grant never widens this).
    """
    from app.domain.errors import Forbidden  # noqa: PLC0415

    now = clock.now()
    members: list[PackageMember] = []
    first_case_id: UUID | None = None
    for document_id in cmd.member_document_ids:
        await authorize(
            Action.DOCUMENT_READ,
            proxy(
                tenant_id=cmd.tenant_id,
                actor_id=cmd.actor_id,
                actor_roles=cmd.actor_roles,
            ),
            Resource(tenant_id=cmd.tenant_id, kind="document", id=document_id),
        )
        document = await resolve_document(
            tenant_id=cmd.tenant_id,
            document_id=document_id,
            document_repo=document_repo,
        )
        if first_case_id is None:
            first_case_id = document.case_id
        sensitivity = str(document.sensitivity.value)
        if sensitivity in {"restricted", "phi"} and not await has_action(
            Action.DOCUMENT_READ_RESTRICTED,
            tenant_id=cmd.tenant_id,
            actor_id=cmd.actor_id,
            actor_roles=cmd.actor_roles,
            resource_id=document_id,
        ):
            raise Forbidden(
                "restricted package member requires document.read_restricted",
                details={"reason": "restricted_member"},
            )
        members.append(
            PackageMember(
                document_id=document.id,
                version_number=document.current_version,
                sha256=document.sha256,
                size=document.size,
            )
        )
    package = DocPackage.create(
        tenant_id=cmd.tenant_id,
        name=cmd.name,
        members=tuple(members),
        created_by=cmd.actor_id,
        now=now,
    )
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=DOC_PACKAGE_BUILT,
        actor_id=cmd.actor_id,
        subject_type=SUBJECT_TYPE_DOCUMENT,
        subject_id=cmd.member_document_ids[0],
        case_id=first_case_id,
        payload=package.manifest(),
        now=now,
    )
    return package


async def find_duplicates(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    case_id: UUID,
    document_repo: DocumentRepository,
) -> tuple[tuple[str, ...], ...]:
    """Cluster one case's documents by content hash (read-only).

    Authorizes ``DOCUMENT_READ`` on the case scope; returns groups of
    document-id strings sharing a digest (acceptance:
    "duplicate-by-hash detected"). Singletons are omitted by the
    domain rule.
    """
    await authorize(
        Action.DOCUMENT_READ,
        proxy(tenant_id=tenant_id, actor_id=actor_id, actor_roles=actor_roles),
        Resource(tenant_id=tenant_id, kind="document", id=case_id),
    )
    documents = await document_repo.list_for_case(tenant_id=tenant_id, case_id=case_id)
    groups = find_duplicate_groups(
        tuple(DedupCandidate(document_id=doc.id, sha256=doc.sha256) for doc in documents)
    )
    return tuple(tuple(str(doc_id) for doc_id in group) for group in groups)


@dataclass(frozen=True, slots=True)
class ExportCsvCommand:
    """Input to :func:`export_csv`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    columns: tuple[str, ...]


async def export_csv(
    cmd: ExportCsvCommand,
    *,
    document_repo: DocumentRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> str:
    """Render an authorized CSV export of one case's documents.

    Restricted rows are included only when the actor holds
    ``document.read_restricted``; otherwise they drop silently (the
    exporter cannot distinguish "absent" from "withheld"). The export
    itself is audited with the column list + emitted row count (never
    row content).
    """
    await authorize(
        Action.DOCUMENT_READ,
        proxy(tenant_id=cmd.tenant_id, actor_id=cmd.actor_id, actor_roles=cmd.actor_roles),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.case_id),
    )
    include_restricted = await has_action(
        Action.DOCUMENT_READ_RESTRICTED,
        tenant_id=cmd.tenant_id,
        actor_id=cmd.actor_id,
        actor_roles=cmd.actor_roles,
        resource_id=cmd.case_id,
    )
    documents = await document_repo.list_for_case(tenant_id=cmd.tenant_id, case_id=cmd.case_id)
    rows = tuple(
        CsvRow(
            values={
                "document_id": str(doc.id),
                "case_id": str(doc.case_id),
                "file_name": doc.file_name,
                "classification": doc.classification,
                "sensitivity": str(doc.sensitivity.value),
                "sha256": doc.sha256,
                "size": str(doc.size),
                "current_version": str(doc.current_version),
            },
            sensitivity=str(doc.sensitivity.value),
        )
        for doc in documents
    )
    export = build_csv_export(
        columns=cmd.columns,
        rows=rows,
        include_restricted=include_restricted,
    )
    now = clock.now()
    await append_audit_and_outbox(
        audit=audit,
        outbox=outbox,
        tenant_id=cmd.tenant_id,
        event_type=CSV_EXPORTED,
        actor_id=cmd.actor_id,
        subject_type=SUBJECT_TYPE_DOCUMENT,
        subject_id=cmd.case_id,
        case_id=cmd.case_id,
        payload={
            "columns": list(cmd.columns),
            "row_count": export.row_count,
            "include_restricted": include_restricted,
        },
        now=now,
    )
    return export.to_text()
