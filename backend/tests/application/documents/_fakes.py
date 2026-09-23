"""Fakes for the upload_document use case tests (P03-US1 / FM-022 / FR-005).

In-memory implementations of the domain ports the use case depends
on. The DocumentStore fake stores bytes in a dict; the
DocumentRepository fake records the staged (Document, DocumentVersion)
pairs so the test can assert exactly what the use case committed.

Following AGENTS.md §4.R5: deterministic, no shared state between
tests, named after the unit under test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.domain.document import Document, DocumentRedaction, DocumentVersion
from app.domain.errors import DependencyUnavailable


@dataclass
class StoredBlob:
    tenant_id: UUID
    content: bytes
    sha256: str
    storage_ref: str


class InMemoryDocumentStore:
    """In-memory ``DocumentStore``.

    Records every ``put`` call so the test can assert that bytes
    landed at the configured ``storage_ref`` and not elsewhere. The
    ``storage_ref`` shape mirrors the local filesystem layout's
    ``<tenant_id>/<sha256[0:2]>/<sha256>`` so the test catches
    layout regressions.
    """

    def __init__(self) -> None:
        self.puts: list[StoredBlob] = []
        self._by_ref: dict[tuple[UUID, str], bytes] = {}

    async def put(
        self,
        *,
        tenant_id: UUID,
        content: bytes,
        content_type: str,
        sha256: str,
    ) -> str:
        storage_ref = f"{tenant_id}/{sha256[:2]}/{sha256}"
        self.puts.append(
            StoredBlob(
                tenant_id=tenant_id,
                content=content,
                sha256=sha256,
                storage_ref=storage_ref,
            )
        )
        self._by_ref[(tenant_id, storage_ref)] = content
        return storage_ref

    async def get(self, *, tenant_id: UUID, content_ref: str) -> bytes:
        return self._by_ref[(tenant_id, content_ref)]


class FailingDocumentStore:
    """A DocumentStore whose ``put`` always raises ``DependencyUnavailable``.

    Used by the storage-unavailable-rollback scenario.
    """

    def __init__(self, message: str = "storage offline") -> None:
        self._message = message
        self.puts: list[dict[str, Any]] = []

    async def put(
        self,
        *,
        tenant_id: UUID,
        content: bytes,
        content_type: str,
        sha256: str,
    ) -> str:
        self.puts.append(
            {
                "tenant_id": tenant_id,
                "sha256": sha256,
                "size": len(content),
            }
        )
        raise DependencyUnavailable(self._message)

    async def get(self, *, tenant_id: UUID, content_ref: str) -> bytes:
        raise DependencyUnavailable(self._message)


class InMemoryDocumentRepository:
    """In-memory ``DocumentRepository``.

    Captures every ``add_document_and_version`` and ``add_version``
    call so the test can assert that exactly one (Document,
    DocumentVersion) pair is staged on the initial upload and exactly
    one additional (Document, DocumentVersion) pair is staged on each
    add-version flow.

    State storage: a dict keyed by ``(tenant_id, document_id)`` so the
    tenant-isolation tests can verify that a cross-tenant id surfaces
    as ``None`` from ``get`` and an empty list from ``list_versions``.
    """

    def __init__(self) -> None:
        self.added: list[tuple[Document, DocumentVersion]] = []
        self.versioned: list[tuple[Document, DocumentVersion]] = []
        self.locked: list[Document] = []
        self.redactions: list[DocumentRedaction] = []
        self._docs: dict[tuple[UUID, UUID], Document] = {}
        self._versions: dict[tuple[UUID, UUID], list[DocumentVersion]] = {}
        self._redactions: dict[tuple[UUID, UUID], list[DocumentRedaction]] = {}

    async def add_document_and_version(
        self,
        *,
        document: Document,
        version: DocumentVersion,
    ) -> None:
        self.added.append((document, version))
        self._docs[(document.tenant_id, document.id)] = document
        self._versions.setdefault((document.tenant_id, document.id), []).append(version)

    async def get(self, tenant_id: UUID, document_id: UUID) -> Document | None:
        return self._docs.get((tenant_id, document_id))

    async def list_for_case(self, *, tenant_id: UUID, case_id: UUID) -> list[Document]:
        """Tenant-scoped case listing (T055 duplicate detection)."""
        return [
            doc for (t, _), doc in self._docs.items() if t == tenant_id and doc.case_id == case_id
        ]

    async def add_version(
        self,
        *,
        document: Document,
        version: DocumentVersion,
    ) -> None:
        self.versioned.append((document, version))
        self._docs[(document.tenant_id, document.id)] = document
        self._versions.setdefault((document.tenant_id, document.id), []).append(version)

    async def list_versions(self, tenant_id: UUID, document_id: UUID) -> list[DocumentVersion]:
        return list(self._versions.get((tenant_id, document_id), ()))

    async def save_locked(self, document: Document) -> None:
        """Mirror the SQL adapter's tenant-scoped UPDATE for ``locked``.

        Stores the locked metadata in the in-memory map so a follow-up
        ``get`` reflects the lock flag.
        """
        self.locked.append(document)
        self._docs[(document.tenant_id, document.id)] = document

    async def add_redaction(
        self,
        *,
        redaction: DocumentRedaction,
        supersede_prior: bool,
    ) -> None:
        """Mirror ``SqlDocumentRepository.add_redaction`` for tests.

        When ``supersede_prior`` is True, every ACTIVE redaction for
        the (tenant, document) tuple is marked SUPERSEDED in memory.
        The new row is then appended in ACTIVE.
        """
        key = (redaction.tenant_id, redaction.document_id)
        bucket = self._redactions.setdefault(key, [])
        if supersede_prior:
            for r in bucket:
                if r.status == "ACTIVE":
                    object.__setattr__(r, "status", "SUPERSEDED")
                    object.__setattr__(r, "superseded_at", redaction.created_at)
                    object.__setattr__(r, "superseded_by", redaction.created_by)
        self.redactions.append(redaction)
        bucket.append(redaction)

    async def list_redactions(self, tenant_id: UUID, document_id: UUID) -> list[DocumentRedaction]:
        """Return redaction rows oldest-first (insertion order)."""
        return list(self._redactions.get((tenant_id, document_id), []))

    async def get_active_redaction(
        self, tenant_id: UUID, document_id: UUID
    ) -> DocumentRedaction | None:
        """Return the ACTIVE redaction, if any (insertion-order scan)."""
        for redaction in self._redactions.get((tenant_id, document_id), []):
            if redaction.status == "ACTIVE":
                return redaction
        return None

    async def save_sensitivity(self, document: Document) -> None:
        """Persist a sensitivity change in memory (FM-025 restrict)."""
        self._docs[(document.tenant_id, document.id)] = document


__all__ = [
    "FailingDocumentStore",
    "InMemoryDocumentRepository",
    "InMemoryDocumentStore",
    "StoredBlob",
]
