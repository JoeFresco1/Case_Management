"""Fakes used by the governed GraphQL tests (P13-US11 / FM-057).

These extend the in-memory fakes in ``tests/application/cases/_fakes.py``
with the document-listing helper (``list_for_case``) that the
governed surface requires. The fake is deliberately hermetic — no
DB, no session — so the suite runs under the sqlite / pure-Python
profile (AGENTS.md §4.R1).

# Layering (AGENTS.md §4.R10)

The fakes live in ``tests/`` because they are testing-only. The
production code does not import them. ``app.interfaces.graphql.*``
imports ``app.application.*`` + ``app.domain.*`` only; the fakes
are passed in via :class:`ResolverContext`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.document import Document
from app.domain.ports import DocumentRepository


@dataclass
class StagedDocument:
    """A document staged on the in-memory ``DocumentRepository`` fake."""

    document: Document


class InMemoryDocumentRepository(DocumentRepository):
    """In-memory ``DocumentRepository`` with a ``list_for_case`` method.

    The GraphQL surface requires :meth:`list_for_case`; the production
    SQL adapter will add it in a future slice. The fake here mirrors
    the contract the application layer expects (every method takes
    ``tenant_id`` first; cross-tenant ids are silently dropped).
    """

    def __init__(self) -> None:
        self._docs: list[Document] = []
        self._added: list[StagedDocument] = []

    def add(self, document: Document) -> None:
        """Stage a document row directly (test helper, not part of the port)."""
        self._docs.append(document)

    async def add_document_and_version(
        self,
        *,
        document: Document,
        version: Any,
    ) -> None:
        """Satisfy the protocol; tests that exercise upload use this path."""
        self._docs.append(document)
        self._added.append(StagedDocument(document=document))

    async def get(self, tenant_id: UUID, document_id: UUID) -> Document | None:
        for d in self._docs:
            if d.tenant_id == tenant_id and d.id == document_id:
                return d
        return None

    async def add_version(self, *, document: Document, version: Any) -> None:
        self._docs.append(document)

    async def list_versions(self, tenant_id: UUID, document_id: UUID) -> list[Any]:
        return []

    async def save_locked(self, document: Document) -> None:
        for i, d in enumerate(self._docs):
            if d.id == document.id and d.tenant_id == document.tenant_id:
                self._docs[i] = document
                return

    async def add_redaction(
        self,
        *,
        redaction: Any,
        supersede_prior: bool,
    ) -> None:
        return None

    async def list_redactions(self, tenant_id: UUID, document_id: UUID) -> list[Any]:
        return []

    async def get_active_redaction(self, tenant_id: UUID, document_id: UUID) -> Any | None:
        return None

    async def save_sensitivity(self, document: Document) -> None:
        for i, d in enumerate(self._docs):
            if d.id == document.id and d.tenant_id == document.tenant_id:
                self._docs[i] = document
                return

    async def list_for_case(self, *, tenant_id: UUID, case_id: UUID) -> Sequence[Document]:
        """Return every document attached to ``case_id`` in the actor's tenant.

        Tenant isolation: a document belonging to a different tenant
        is silently dropped (the contract the SQL adapter will
        satisfy via ``WHERE tenant_id = :tenant_id AND case_id = :case_id``).
        """
        return [d for d in self._docs if d.tenant_id == tenant_id and d.case_id == case_id]


def make_document(
    *,
    tenant_id: UUID,
    case_id: UUID,
    classification: str = "Contract",
    sensitivity: str = "internal",
    locked: bool = False,
) -> Document:
    """Build a Document aggregate with deterministic timestamps."""
    now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
    return Document(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id,
        file_name="contract.txt",
        content_type="text/plain",
        classification=classification,
        sensitivity=sensitivity,  # type: ignore[arg-type]
        sha256="0" * 64,
        size=128,
        current_version=1,
        storage_ref="local://contract.txt",
        uploaded_by=uuid4(),
        created_at=now,
        updated_at=now,
        locked=locked,
        locked_at=None,
        locked_by=None,
    )


def make_case_with_restricted_schema(
    *,
    tenant_id: UUID,
    actor_id: UUID,
    title: str = "Title",
    ssn: str = "123-45-6789",
) -> tuple[Case, CaseTypeVersion]:
    """Build a Case + its CaseTypeVersion with one restricted field.

    The schema declares ``ssn`` at ``sensitivity=restricted`` so the
    redactor replaces its value for actors without
    ``Action.CASE_READ_SENSITIVE``. Tests use this helper to keep
    the policy wiring focused on the boundary under test, not the
    case factory.
    """
    version = CaseTypeVersion(
        id=uuid4(),
        tenant_id=tenant_id,
        case_type_id=uuid4(),
        version_number=1,
        schema=CaseTypeSchema(
            fields=(
                CaseTypeField(
                    key="title",
                    label="Title",
                    type="text",
                    required=True,
                ),
                CaseTypeField(
                    key="ssn",
                    label="SSN",
                    type="text",
                    required=False,
                    sensitivity="restricted",
                ),
            ),
            document_classifications=("Contract", "Evidence"),
        ),
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        published_by=actor_id,
    )
    case = Case(
        id=uuid4(),
        tenant_id=tenant_id,
        reference=CaseReference(value="C-ACME-2026-000001"),
        case_type_version_id=version.id,
        status=CaseStatus.OPEN,
        priority="normal",
        title=title,
        description="",
        data={"title": title, "ssn": ssn},
        current_workflow_state=None,
        version_token=1,
        created_at=datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC),
        created_by=actor_id,
        closed_at=None,
        sensitivity="internal",
    )
    # Keep the CaseType row so the application layer's
    # case_type_version_repo.get can resolve the schema. We register
    # the version against a synthesized CaseType for the same
    # tenant. The test passes a matching version repo (see
    # ``InMemoryCaseTypeVersionRepository``) so this side-channel is
    # not exercised — but the helper makes the wiring clear.
    _ = CaseType(
        id=version.case_type_id,
        tenant_id=tenant_id,
        key="ServiceRequest",
        name="Service Request",
        description="",
        current_version_id=version.id,
    )
    return case, version


__all__ = [
    "InMemoryDocumentRepository",
    "StagedDocument",
    "make_case_with_restricted_schema",
    "make_document",
]
