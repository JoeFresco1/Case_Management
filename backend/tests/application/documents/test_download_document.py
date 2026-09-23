"""Tests for tenant-scoped document download and access auditing."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.application.documents.download_document import (
    DOCUMENT_ACCESSED,
    DownloadDocumentCommand,
    handle,
)
from app.domain.document import Document, DocumentSensitivity
from app.domain.errors import Forbidden, NotFound
from tests.application.cases._fakes import FixedClock, InMemoryAuditStore
from tests.application.documents._fakes import InMemoryDocumentRepository, InMemoryDocumentStore

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
ACTOR = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
DOC_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _document() -> Document:
    """Build deterministic document metadata for the download tests."""
    return Document(
        id=DOC_ID,
        tenant_id=TENANT,
        case_id=UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
        file_name="evidence.pdf",
        content_type="application/pdf",
        classification="Evidence",
        sensitivity=DocumentSensitivity.INTERNAL,
        sha256="a" * 64,
        size=7,
        current_version=1,
        storage_ref="ref",
        uploaded_by=ACTOR,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_download_document__authorized__returns_bytes_and_metadata_audit() -> None:
    repo = InMemoryDocumentRepository()
    doc = _document()
    repo._docs[(TENANT, DOC_ID)] = doc
    store = InMemoryDocumentStore()
    await store.put(
        tenant_id=TENANT,
        content=b"payload",
        content_type=doc.content_type,
        sha256=doc.sha256,
    )
    # The fake's content-addressed ref differs only in the tenant prefix.
    store._by_ref[(TENANT, "ref")] = b"payload"
    audit = InMemoryAuditStore()

    result = await handle(
        DownloadDocumentCommand(TENANT, ACTOR, ("reviewer",), DOC_ID, "corr-1"),
        document_repo=repo,
        document_store=store,
        audit=audit,
        clock=FixedClock(NOW),
    )

    assert result.content == b"payload"
    assert audit.events[0].event_type == DOCUMENT_ACCESSED
    assert audit.events[0].payload == {
        "document_id": str(DOC_ID),
        "version": 1,
        "rendition": "original",
    }
    assert "payload" not in repr(audit.events[0].payload)


@pytest.mark.asyncio
async def test_download_document__cross_tenant__returns_not_found() -> None:
    with pytest.raises(NotFound):
        await handle(
            DownloadDocumentCommand(OTHER_TENANT, ACTOR, ("reviewer",), DOC_ID),
            document_repo=InMemoryDocumentRepository(),
            document_store=InMemoryDocumentStore(),
            audit=InMemoryAuditStore(),
            clock=FixedClock(NOW),
        )


@pytest.mark.asyncio
async def test_download_document__unauthorized__denies_before_storage() -> None:
    with pytest.raises(Forbidden):
        await handle(
            DownloadDocumentCommand(TENANT, ACTOR, ("intake",), DOC_ID),
            document_repo=InMemoryDocumentRepository(),
            document_store=InMemoryDocumentStore(),
            audit=InMemoryAuditStore(),
            clock=FixedClock(NOW),
        )
