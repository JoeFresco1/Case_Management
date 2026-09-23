"""Tests for FM-025 restrict + rendition-hardened download (T5 acceptance).

Covers the task acceptance criteria:

- guessed-URL download of a restricted document returns ``Forbidden``
  with no metadata leak (no file name, storage ref, or bytes in
  ``details``);
- both the original and the redacted rendition append their own
  ``DocumentAccessed`` audit row (independently auditable views);
- two-tenant isolation: a cross-tenant id surfaces as ``NotFound``
  for restrict / download / redacted download.

Naming follows ``test_<unit>_<scenario>__<expectation>`` (AGENTS.md
§4.R5). Structure is ``// given`` / ``// when`` / ``// then``.
Deterministic: fixed clock, fixed UUIDs, in-memory fakes only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.application.documents import restrict_document
from app.application.documents.download_document import (
    DOCUMENT_ACCESSED,
    DownloadDocumentCommand,
)
from app.application.documents.download_document import (
    handle as download,
)
from app.application.documents.protect_document import (
    RedactDocumentCommand,
    redact,
)
from app.application.documents.restrict_document import (
    DOCUMENT_RESTRICTED,
    RestrictDocumentCommand,
    restrict,
)
from app.domain.document import Document, DocumentSensitivity, DocumentVersion
from app.domain.errors import DocumentLocked, Forbidden, NotFound, ValidationFailed
from tests.application.cases._fakes import FixedClock, InMemoryAuditStore, InMemoryOutbox
from tests.application.documents._fakes import (
    InMemoryDocumentRepository,
    InMemoryDocumentStore,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
SUPERVISOR = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
REVIEWER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
INTAKE = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CASE_A = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _document(
    *,
    tenant: UUID = TENANT_A,
    sensitivity: DocumentSensitivity = DocumentSensitivity.INTERNAL,
    locked: bool = False,
) -> tuple[Document, DocumentVersion, bytes]:
    """Build a v1 document + version + bytes triple."""
    content = b"%PDF-1.4\nsensitive bytes"
    sha = hashlib.sha256(content).hexdigest()
    ref = f"{tenant}/{sha[:2]}/{sha}"
    doc = Document.create(
        tenant_id=tenant,
        case_id=CASE_A,
        file_name="onboarding.pdf",
        content_type="application/pdf",
        classification="Contract",
        sensitivity=sensitivity,
        sha256=sha,
        size=len(content),
        storage_ref=ref,
        uploaded_by=SUPERVISOR,
        now=NOW,
    )
    if locked:
        doc = doc.with_lock(actor_id=SUPERVISOR, now=NOW)
    version = DocumentVersion(
        id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        tenant_id=tenant,
        document_id=doc.id,
        version_number=1,
        sha256=sha,
        size=len(content),
        storage_ref=ref,
        content_type="application/pdf",
        uploaded_by=SUPERVISOR,
        uploaded_at=NOW,
    )
    return doc, version, content


def _seed(
    doc: Document, version: DocumentVersion, content: bytes
) -> tuple[InMemoryDocumentRepository, InMemoryDocumentStore]:
    """Seed the repo + store with one document's original bytes."""
    repo = InMemoryDocumentRepository()
    repo.added.append((doc, version))
    repo._docs[(doc.tenant_id, doc.id)] = doc  # noqa: SLF001
    repo._versions.setdefault((doc.tenant_id, doc.id), []).append(version)  # noqa: SLF001
    store = InMemoryDocumentStore()
    store._by_ref[(doc.tenant_id, doc.storage_ref)] = content  # noqa: SLF001
    return repo, store


# ---------------------------------------------------------------------------
# Restrict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restrict_document__happy_path__flips_label_and_audits() -> None:
    # // given an internal document
    doc, version, content = _document()
    repo, _store = _seed(doc, version, content)
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    # // when a supervisor restricts it
    result = await restrict(
        RestrictDocumentCommand(
            tenant_id=TENANT_A,
            actor_id=SUPERVISOR,
            actor_roles=("supervisor",),
            document_id=doc.id,
            sensitivity=DocumentSensitivity.RESTRICTED,
        ),
        document_repo=repo,
        audit=audit,
        outbox=outbox,
        clock=FixedClock(NOW),
    )
    # // then the label flips, history is preserved, audit + outbox land
    assert result.document.sensitivity is DocumentSensitivity.RESTRICTED
    assert result.previous_sensitivity is DocumentSensitivity.INTERNAL
    current = await repo.get(TENANT_A, doc.id)
    assert current is not None
    assert current.sensitivity is DocumentSensitivity.RESTRICTED
    assert audit.events[0].event_type == DOCUMENT_RESTRICTED
    assert audit.events[0].payload["previous_sensitivity"] == "internal"
    assert audit.events[0].payload["sensitivity"] == "restricted"
    assert outbox.events[0].event_type == DOCUMENT_RESTRICTED


@pytest.mark.asyncio
async def test_restrict_document__unauthorized__raises_forbidden() -> None:
    # // given an internal document
    doc, version, content = _document()
    repo, _store = _seed(doc, version, content)
    # // when a reviewer (no redact grant) restricts
    # // then Forbidden, no state change
    with pytest.raises(Forbidden):
        await restrict(
            RestrictDocumentCommand(
                tenant_id=TENANT_A,
                actor_id=REVIEWER,
                actor_roles=("reviewer",),
                document_id=doc.id,
                sensitivity=DocumentSensitivity.RESTRICTED,
            ),
            document_repo=repo,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(NOW),
        )


@pytest.mark.asyncio
async def test_restrict_document__downgrade_target__raises_validation_failed() -> None:
    # // given a restricted document
    doc, version, content = _document(sensitivity=DocumentSensitivity.RESTRICTED)
    repo, _store = _seed(doc, version, content)
    # // when declassification is attempted via restrict
    # // then ValidationFailed names the sensitivity field (fail-closed)
    with pytest.raises(ValidationFailed) as exc_info:
        await restrict(
            RestrictDocumentCommand(
                tenant_id=TENANT_A,
                actor_id=SUPERVISOR,
                actor_roles=("supervisor",),
                document_id=doc.id,
                sensitivity=DocumentSensitivity.INTERNAL,
            ),
            document_repo=repo,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(NOW),
        )
    assert exc_info.value.field == "sensitivity"


@pytest.mark.asyncio
async def test_restrict_document__locked__raises_document_locked() -> None:
    # // given a filed document
    doc, version, content = _document(locked=True)
    repo, _store = _seed(doc, version, content)
    # // when restriction is attempted
    # // then DocumentLocked before any write
    with pytest.raises(DocumentLocked):
        await restrict(
            RestrictDocumentCommand(
                tenant_id=TENANT_A,
                actor_id=SUPERVISOR,
                actor_roles=("supervisor",),
                document_id=doc.id,
                sensitivity=DocumentSensitivity.PHI,
            ),
            document_repo=repo,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(NOW),
        )


@pytest.mark.asyncio
async def test_restrict_document__cross_tenant__raises_not_found() -> None:
    # // given a document owned by tenant A
    doc, version, content = _document(tenant=TENANT_A)
    repo, _store = _seed(doc, version, content)
    # // when tenant B restricts the same id
    # // then NotFound (no existence oracle across tenants)
    with pytest.raises(NotFound):
        await restrict(
            RestrictDocumentCommand(
                tenant_id=TENANT_B,
                actor_id=SUPERVISOR,
                actor_roles=("supervisor",),
                document_id=doc.id,
                sensitivity=DocumentSensitivity.RESTRICTED,
            ),
            document_repo=repo,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(NOW),
        )


# ---------------------------------------------------------------------------
# Guessed-URL download of a restricted document
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_document__restricted_without_grant__forbidden_without_leak() -> None:
    # // given a restricted document (e.g. after restrict)
    doc, version, content = _document(sensitivity=DocumentSensitivity.RESTRICTED)
    repo, store = _seed(doc, version, content)
    audit = InMemoryAuditStore()
    # // when a view-only reviewer guesses the URL
    with pytest.raises(Forbidden) as exc_info:
        await download(
            DownloadDocumentCommand(TENANT_A, REVIEWER, ("reviewer",), doc.id, "corr-1"),
            document_repo=repo,
            document_store=store,
            audit=audit,
            clock=FixedClock(NOW),
        )
    # // then Forbidden carries only a safe reason — no file name,
    # // storage ref, or bytes a probe could reuse; nothing audited
    blob = json.dumps(exc_info.value.details, default=str)
    assert "onboarding.pdf" not in blob
    assert doc.storage_ref not in blob
    assert content.decode(errors="ignore") not in blob
    assert exc_info.value.details.get("reason") == "restricted_document"
    assert audit.events == []


@pytest.mark.asyncio
async def test_download_document__restricted_with_grant__returns_bytes_and_audits() -> None:
    # // given a restricted document
    doc, version, content = _document(sensitivity=DocumentSensitivity.RESTRICTED)
    repo, store = _seed(doc, version, content)
    audit = InMemoryAuditStore()
    # // when a supervisor (stricter grant) downloads the original
    result = await download(
        DownloadDocumentCommand(TENANT_A, SUPERVISOR, ("supervisor",), doc.id, "corr-1"),
        document_repo=repo,
        document_store=store,
        audit=audit,
        clock=FixedClock(NOW),
    )
    # // then bytes + a rendition-marked audit row
    assert result.content == content
    assert result.rendition == "original"
    assert audit.events[0].event_type == DOCUMENT_ACCESSED
    assert audit.events[0].payload["rendition"] == "original"


@pytest.mark.asyncio
async def test_download_document__cross_tenant__raises_not_found() -> None:
    # // given a document owned by tenant A
    doc, version, content = _document(tenant=TENANT_A)
    repo, store = _seed(doc, version, content)
    # // when tenant B guesses the id
    # // then NotFound (never Forbidden — no existence oracle)
    with pytest.raises(NotFound):
        await download(
            DownloadDocumentCommand(TENANT_B, SUPERVISOR, ("supervisor",), doc.id),
            document_repo=repo,
            document_store=store,
            audit=InMemoryAuditStore(),
            clock=FixedClock(NOW),
        )


# ---------------------------------------------------------------------------
# Original + redaction renditions are each audited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_document__original_and_redaction__both_audited() -> None:
    # // given an internal document with one ACTIVE redaction
    doc, version, content = _document()
    repo, store = _seed(doc, version, content)
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()
    redacted_bytes = b"%PDF-1.4\nREDACTED view"
    await redact(
        RedactDocumentCommand(
            tenant_id=TENANT_A,
            actor_id=SUPERVISOR,
            actor_roles=("supervisor",),
            document_id=doc.id,
            base_version_number=1,
            content=redacted_bytes,
            content_type="application/pdf",
            reason="ssn blacked out",
            region=None,
        ),
        document_repo=repo,
        document_store=store,
        audit=audit,
        outbox=outbox,
        clock=FixedClock(NOW),
    )
    # // when the supervisor downloads the redacted rendition ...
    redacted = await download(
        DownloadDocumentCommand(
            TENANT_A, SUPERVISOR, ("supervisor",), doc.id, "corr-r", "redacted"
        ),
        document_repo=repo,
        document_store=store,
        audit=audit,
        clock=FixedClock(NOW),
    )
    assert redacted.content == redacted_bytes
    assert redacted.rendition == "redacted"
    assert redacted.redaction is not None
    # // ... and the original rendition
    original = await download(
        DownloadDocumentCommand(
            TENANT_A, SUPERVISOR, ("supervisor",), doc.id, "corr-o", "original"
        ),
        document_repo=repo,
        document_store=store,
        audit=audit,
        clock=FixedClock(NOW),
    )
    assert original.content == content
    # // then both views carry their own audit row (redaction row + 2 reads)
    renditions = [
        e.payload.get("rendition") for e in audit.events if e.event_type == DOCUMENT_ACCESSED
    ]
    assert renditions == ["redacted", "original"]
    assert audit.events[1].payload["redaction_id"] == str(redacted.redaction.id)


@pytest.mark.asyncio
async def test_download_document__original_needs_stricter_after_redaction() -> None:
    # // given an internal parent with an ACTIVE redaction
    doc, version, content = _document()
    repo, store = _seed(doc, version, content)
    await redact(
        RedactDocumentCommand(
            tenant_id=TENANT_A,
            actor_id=SUPERVISOR,
            actor_roles=("supervisor",),
            document_id=doc.id,
            base_version_number=1,
            content=b"%PDF-1.4\nREDACTED",
            content_type="application/pdf",
            reason="ssn blacked out",
            region=None,
        ),
        document_repo=repo,
        document_store=store,
        audit=InMemoryAuditStore(),
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )
    # // when a view-only reviewer asks for the original bytes
    # // then Forbidden under the original-specific reason (the view
    # // stays readable, but the removed bytes do not leak)
    with pytest.raises(Forbidden) as exc_info:
        await download(
            DownloadDocumentCommand(TENANT_A, REVIEWER, ("reviewer",), doc.id, "corr-o"),
            document_repo=repo,
            document_store=store,
            audit=InMemoryAuditStore(),
            clock=FixedClock(NOW),
        )
    assert exc_info.value.details.get("reason") == "original_requires_stricter_right"


@pytest.mark.asyncio
async def test_download_document__redacted_cross_tenant__raises_not_found() -> None:
    # // given a redacted document owned by tenant A
    doc, version, content = _document(tenant=TENANT_A)
    repo, store = _seed(doc, version, content)
    await redact(
        RedactDocumentCommand(
            tenant_id=TENANT_A,
            actor_id=SUPERVISOR,
            actor_roles=("supervisor",),
            document_id=doc.id,
            base_version_number=1,
            content=b"%PDF-1.4\nREDACTED",
            content_type="application/pdf",
            reason="ssn blacked out",
            region=None,
        ),
        document_repo=repo,
        document_store=store,
        audit=InMemoryAuditStore(),
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )
    # // when tenant B asks for the redacted rendition
    # // then NotFound (no cross-tenant rendition oracle)
    with pytest.raises(NotFound):
        await download(
            DownloadDocumentCommand(TENANT_B, SUPERVISOR, ("supervisor",), doc.id, "", "redacted"),
            document_repo=repo,
            document_store=store,
            audit=InMemoryAuditStore(),
            clock=FixedClock(NOW),
        )


__all__ = ["restrict_document"]
