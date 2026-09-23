"""SQL adapter for the Document + DocumentVersion aggregate (P03-US1, US2 / FM-022, FM-023).

The adapter implements the ``DocumentRepository`` Protocol defined in
``app/domain/ports.py``. Tenant isolation is explicit on every method
(`WHERE tenant_id = :tenant_id`); the listener-based defense-in-depth
in ``config.TENANT_LISTENER_ENABLED`` is never the sole mechanism.

Bytes live in the ``DocumentStore`` port (filesystem local, Azure Blob
in production); the relational store carries metadata + version rows
only. ``sha256`` is the canonical content key and is recorded on both
rows so an audit replay can reconstruct "which bytes were stored when".

# INTEGRATION_SEAM

This is the local SQLAlchemy-backed adapter. An Azure SQL adapter
would implement the same Protocol and live under
``app/infrastructure/persistence/azure_sql_documents.py``; no domain
or application code changes are required.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.document import (
    Document,
    DocumentRedaction,
    DocumentSensitivity,
    DocumentVersion,
)
from app.infrastructure.persistence.models import (
    Document as DocumentRow,
)
from app.infrastructure.persistence.models import (
    DocumentRedaction as DocumentRedactionRow,
)
from app.infrastructure.persistence.models import (
    DocumentVersion as DocumentVersionRow,
)


def _document_from_row(row: DocumentRow) -> Document:
    """Translate an ORM ``DocumentRow`` into the domain aggregate.

    Kept local so the adapter owns the ORM -> domain boundary. The
    domain has no SQLAlchemy dependency (AGENTS.md §4.R10).
    """
    return Document(
        id=row.id,
        tenant_id=row.tenant_id,
        case_id=row.case_id,
        file_name=row.file_name,
        content_type=row.content_type,
        classification=row.classification,
        sensitivity=DocumentSensitivity(row.sensitivity),
        sha256=row.sha256,
        size=row.size,
        current_version=row.current_version,
        storage_ref=row.storage_ref,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        locked=row.locked,
        locked_at=row.locked_at,
        locked_by=row.locked_by,
    )


def _version_from_row(row: DocumentVersionRow) -> DocumentVersion:
    """Translate an ORM ``DocumentVersionRow`` into the domain aggregate."""
    return DocumentVersion(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        version_number=row.version_number,
        sha256=row.sha256,
        size=row.size,
        storage_ref=row.storage_ref,
        content_type=row.content_type,
        uploaded_by=row.uploaded_by,
        uploaded_at=row.uploaded_at,
    )


def _redaction_from_row(row: DocumentRedactionRow) -> DocumentRedaction:
    """Translate an ORM ``DocumentRedactionRow`` into the domain aggregate."""
    return DocumentRedaction(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        base_version_number=row.base_version_number,
        reason=row.reason,
        sha256=row.sha256,
        size=row.size,
        storage_ref=row.storage_ref,
        content_type=row.content_type,
        region=row.region,
        status=row.status,
        created_by=row.created_by,
        created_at=row.created_at,
        superseded_at=row.superseded_at,
        superseded_by=row.superseded_by,
    )


class SqlDocumentRepository:
    """SQL-backed DocumentRepository.

    First slice: ``add_document_and_version`` (upload), ``get``,
    ``add_version``, ``list_versions``. Per-version retrieval by id is
    delegated to the application via the version list (FM-033/P03-US3
    search lands later).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add_document_and_version(
        self,
        *,
        document: Document,
        version: DocumentVersion,
    ) -> None:
        """Stage the metadata and version rows on the active session.

        The session is not committed here; the use case boundary
        commits once so the document + audit + outbox land atomically.
        A failed insert raises and the caller rolls the transaction
        back.
        """
        self._s.add(
            DocumentRow(
                id=document.id,
                tenant_id=document.tenant_id,
                case_id=document.case_id,
                file_name=document.file_name,
                content_type=document.content_type,
                classification=document.classification,
                sensitivity=document.sensitivity.value,
                sha256=document.sha256,
                size=document.size,
                current_version=document.current_version,
                storage_ref=document.storage_ref,
                uploaded_by=document.uploaded_by,
                created_at=document.created_at,
                updated_at=document.updated_at,
            )
        )
        self._s.add(
            DocumentVersionRow(
                id=version.id,
                tenant_id=version.tenant_id,
                document_id=version.document_id,
                version_number=version.version_number,
                sha256=version.sha256,
                size=version.size,
                storage_ref=version.storage_ref,
                content_type=version.content_type,
                uploaded_by=version.uploaded_by,
                uploaded_at=version.uploaded_at,
            )
        )

    async def get(self, tenant_id: UUID, document_id: UUID) -> Document | None:
        """Fetch one ``Document`` row by id within the actor's tenant.

        The ``WHERE tenant_id = :tenant_id`` clause is the tenant
        isolation boundary; a cross-tenant id is treated as missing.
        """
        stmt = select(DocumentRow).where(
            DocumentRow.id == document_id,
            DocumentRow.tenant_id == tenant_id,
        )
        result = await self._s.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return _document_from_row(row)

    async def list_for_case(self, *, tenant_id: UUID, case_id: UUID) -> list[Document]:
        """Return every document attached to ``case_id`` (T055).

        Tenant-scoped; a cross-tenant ``case_id`` returns the empty
        list. Used by the duplicate-upload check to compare digests.
        """
        stmt = (
            select(DocumentRow)
            .where(
                DocumentRow.tenant_id == tenant_id,
                DocumentRow.case_id == case_id,
            )
            .order_by(DocumentRow.created_at.asc())
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_document_from_row(row) for row in rows]

    async def add_version(
        self,
        *,
        document: Document,
        version: DocumentVersion,
    ) -> None:
        """Append a ``DocumentVersion`` row and bump ``current_version``.

        Why one atomic method (not two): the metadata pointer and the
        new version row MUST commit together so a reader never sees
        ``current_version = N`` without the corresponding row in
        ``document_versions``. The application use case commits the
        transaction that also contains the audit + outbox rows, so
        the four writes are atomic end-to-end.

        Why we touch ``sha256``/``size``/``storage_ref`` on the
        metadata row: the metadata row exposes the *current* version's
        digest and ref so the case workspace can render the active
        document without an extra join. The per-version rows remain
        the source of truth for history (P03-US2).
        """
        self._s.add(
            DocumentVersionRow(
                id=version.id,
                tenant_id=version.tenant_id,
                document_id=version.document_id,
                version_number=version.version_number,
                sha256=version.sha256,
                size=version.size,
                storage_ref=version.storage_ref,
                content_type=version.content_type,
                uploaded_by=version.uploaded_by,
                uploaded_at=version.uploaded_at,
            )
        )
        # Update the parent document's "current" pointer + content
        # summary in the SAME transaction as the version-row insert.
        # Tenant scoping on the UPDATE prevents a cross-tenant version
        # add from sneaking through.
        stmt = (
            update(DocumentRow)
            .where(
                DocumentRow.id == document.id,
                DocumentRow.tenant_id == document.tenant_id,
            )
            .values(
                current_version=document.current_version,
                sha256=document.sha256,
                size=document.size,
                storage_ref=document.storage_ref,
                updated_at=document.updated_at,
            )
        )
        await self._s.execute(stmt)

    async def list_versions(self, tenant_id: UUID, document_id: UUID) -> list[DocumentVersion]:
        """Return all version rows for a document in version order.

        The query is tenant-scoped on both the join and the document
        row so a cross-tenant document id returns the empty list (the
        caller decides whether to surface that as 404).
        """
        stmt = (
            select(DocumentVersionRow)
            .where(
                DocumentVersionRow.document_id == document_id,
                DocumentVersionRow.tenant_id == tenant_id,
            )
            .order_by(DocumentVersionRow.version_number.asc())
        )
        result = await self._s.execute(stmt)
        rows = result.scalars().all()
        return [_version_from_row(r) for r in rows]

    async def save_locked(self, document: Document) -> None:
        """Persist the ``locked`` flag + actor + timestamp on the parent row.

        Tenant-scoped UPDATE: ``WHERE tenant_id = :tenant_id`` is the
        binding tenant check (FM-002). The session is not committed
        here; the use case boundary commits once so the lock flip +
        audit + outbox land atomically.
        """
        stmt = (
            update(DocumentRow)
            .where(
                DocumentRow.id == document.id,
                DocumentRow.tenant_id == document.tenant_id,
            )
            .values(
                locked=document.locked,
                locked_at=document.locked_at,
                locked_by=document.locked_by,
                updated_at=document.updated_at,
            )
        )
        await self._s.execute(stmt)

    async def add_redaction(
        self,
        *,
        redaction: DocumentRedaction,
        supersede_prior: bool,
    ) -> None:
        """Insert a redaction row + (optionally) supersede the prior ACTIVE one.

        The supersession UPDATE runs first so a reader never sees two
        ACTIVE rows on the same document in the same transaction. Both
        writes share the session so the caller commits once.
        """
        if supersede_prior:
            stmt = (
                update(DocumentRedactionRow)
                .where(
                    DocumentRedactionRow.tenant_id == redaction.tenant_id,
                    DocumentRedactionRow.document_id == redaction.document_id,
                    DocumentRedactionRow.status == "ACTIVE",
                )
                .values(
                    status="SUPERSEDED",
                    superseded_at=redaction.created_at,
                    superseded_by=redaction.created_by,
                )
            )
            await self._s.execute(stmt)
        self._s.add(
            DocumentRedactionRow(
                id=redaction.id,
                tenant_id=redaction.tenant_id,
                document_id=redaction.document_id,
                base_version_number=redaction.base_version_number,
                reason=redaction.reason,
                sha256=redaction.sha256,
                size=redaction.size,
                storage_ref=redaction.storage_ref,
                content_type=redaction.content_type,
                region=redaction.region,
                status=redaction.status,
                created_by=redaction.created_by,
                created_at=redaction.created_at,
                superseded_at=redaction.superseded_at,
                superseded_by=redaction.superseded_by,
            )
        )

    async def list_redactions(self, tenant_id: UUID, document_id: UUID) -> list[DocumentRedaction]:
        """Return all redaction rows for a document, oldest first.

        Tenant-scoped on both columns so a cross-tenant id returns the
        empty list (the caller surfaces it as 404 without an oracle).
        """
        stmt = (
            select(DocumentRedactionRow)
            .where(
                DocumentRedactionRow.tenant_id == tenant_id,
                DocumentRedactionRow.document_id == document_id,
            )
            .order_by(DocumentRedactionRow.created_at.asc())
        )
        result = await self._s.execute(stmt)
        rows = result.scalars().all()
        return [_redaction_from_row(r) for r in rows]

    async def get_active_redaction(
        self, tenant_id: UUID, document_id: UUID
    ) -> DocumentRedaction | None:
        """Return the ACTIVE redaction for a document, if any.

        Tenant-scoped; ``None`` for missing / non-active / cross-tenant.
        """
        stmt = select(DocumentRedactionRow).where(
            DocumentRedactionRow.tenant_id == tenant_id,
            DocumentRedactionRow.document_id == document_id,
            DocumentRedactionRow.status == "ACTIVE",
        )
        result = await self._s.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return _redaction_from_row(row)

    async def save_sensitivity(self, document: Document) -> None:
        """Persist a sensitivity change on the parent row (FM-025 restrict).

        Tenant-scoped UPDATE; the caller commits once so the flip +
        audit + outbox land atomically.
        """
        stmt = (
            update(DocumentRow)
            .where(
                DocumentRow.id == document.id,
                DocumentRow.tenant_id == document.tenant_id,
            )
            .values(
                sensitivity=document.sensitivity.value,
                updated_at=document.updated_at,
            )
        )
        await self._s.execute(stmt)


__all__ = ["SqlDocumentRepository"]
