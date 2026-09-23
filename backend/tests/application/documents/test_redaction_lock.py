"""Tests for ``app.application.documents.protect_document`` (P03-US6, US8 / FM-025).

Acceptance scenarios covered:

- ``redact_document`` happy path: a new versioned redaction is appended
  while the original ``Document`` aggregate + ``DocumentVersion`` rows
  remain retrievable (preserved).
- A prior ACTIVE redaction is SUPERSEDED in the same transaction so the
  timeline always shows exactly one ACTIVE redaction per document.
- ``lock_document`` happy path: a document is locked; subsequent
  redactions / versions raise ``DocumentLocked`` (mapped to HTTP 423
  by the router).
- The lock operation appends a ``DocumentLocked`` audit row.
- Tenant isolation: a cross-tenant redaction / lock surfaces as
  ``NotFound`` (no leak of existence across tenants).

Naming follows ``test_<unit>_<scenario>__<expectation>``
(AGENTS.md §4.R5). Tests are deterministic: a fixed clock, fixed UUIDs.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.documents import protect_document
from app.application.documents.protect_document import (
    DOCUMENT_LOCKED,
    REDACTION_CREATED,
    LockDocumentCommand,
    RedactDocumentCommand,
    lock,
    redact,
)
from app.application.documents.version_document import (
    VersionDocumentCommand,
)
from app.application.documents.version_document import (
    handle as add_version,
)
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.document import (
    Document,
    DocumentRedaction,
    DocumentSensitivity,
    DocumentVersion,
)
from app.domain.errors import (
    DependencyUnavailable,
    DocumentLocked,
    Forbidden,
    NotFound,
    ValidationFailed,
)
from tests.application.cases._fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryCaseRepository,
    InMemoryCaseTypeRepository,
    InMemoryCaseTypeVersionRepository,
    InMemoryOutbox,
)
from tests.application.documents._fakes import (
    FailingDocumentStore,
    InMemoryDocumentRepository,
    InMemoryDocumentStore,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
REVIEWER_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
INTAKE_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SUPERVISOR_USER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


def _seeded_world(*, locked: bool = False) -> dict[str, Any]:
    """Build a tenant + persisted case + v1 document ready for redaction / lock tests."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()

    schema = CaseTypeSchema(
        fields=(CaseTypeField(key="title", label="Title", type="text", required=True),),
        document_classifications=("Contract", "Correspondence", "Evidence"),
        rules=(),
        document_max_size_bytes=4096,
        document_allowed_content_types=("application/pdf",),
    )
    version = CaseTypeVersion(
        id=uuid4(),
        tenant_id=ACME_TENANT,
        case_type_id=uuid4(),
        version_number=1,
        schema=schema,
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=FIXED_NOW,
        published_by=uuid4(),
        created_at=FIXED_NOW,
    )
    case_type = CaseType(
        id=version.case_type_id,
        tenant_id=ACME_TENANT,
        key="ServiceRequest",
        name="Service Request",
        description="",
        sensitivity_default="internal",
        current_version_id=version.id,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    case_type_repo.add(case_type)
    version_repo.add(version)

    case = Case.create(
        tenant_id=ACME_TENANT,
        reference=CaseReference("C-ACME-2026-000001"),
        case_type_version=version,
        title="Need onboarding help",
        description="",
        data={"title": "Need onboarding help"},
        priority="normal",
        created_by=REVIEWER_USER,
        now=FIXED_NOW,
    )
    case_repo._cases.append(case)  # noqa: SLF001

    initial_content = b"%PDF-1.4\nv1 bytes"
    initial_sha = hashlib.sha256(initial_content).hexdigest()
    initial_ref = f"{ACME_TENANT}/{initial_sha[:2]}/{initial_sha}"
    document = Document.create(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="onboarding.pdf",
        content_type="application/pdf",
        classification="Contract",
        sensitivity=DocumentSensitivity.INTERNAL,
        sha256=initial_sha,
        size=len(initial_content),
        storage_ref=initial_ref,
        uploaded_by=REVIEWER_USER,
        now=FIXED_NOW,
    )
    if locked:
        document = document.with_lock(actor_id=SUPERVISOR_USER, now=FIXED_NOW)
    initial_version = DocumentVersion(
        id=uuid4(),
        tenant_id=ACME_TENANT,
        document_id=document.id,
        version_number=1,
        sha256=initial_sha,
        size=len(initial_content),
        storage_ref=initial_ref,
        content_type="application/pdf",
        uploaded_by=REVIEWER_USER,
        uploaded_at=FIXED_NOW,
    )

    return {
        "case_repo": case_repo,
        "case_type_repo": case_type_repo,
        "version_repo": version_repo,
        "case": case,
        "case_type": case_type,
        "version": version,
        "document": document,
        "initial_version": initial_version,
        "initial_content": initial_content,
    }


def _seed_doc_repo(
    doc_repo: InMemoryDocumentRepository,
    document: Document,
    initial_version: DocumentVersion,
) -> None:
    doc_repo.added.append((document, initial_version))
    doc_repo._docs[(document.tenant_id, document.id)] = document  # noqa: SLF001
    doc_repo._versions.setdefault(  # noqa: SLF001
        (document.tenant_id, document.id), []
    ).append(initial_version)


def _base_redact_cmd(
    *,
    document_id: UUID,
    tenant_id: UUID = ACME_TENANT,
    actor_id: UUID = SUPERVISOR_USER,
    actor_roles: tuple[str, ...] = ("supervisor",),
    base_version_number: int = 1,
    content: bytes = b"%PDF-1.4\nREDACTED",
    reason: str = "ssn redacted",
    region: str | None = "page 2 paragraph 3",
) -> RedactDocumentCommand:
    return RedactDocumentCommand(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_roles=actor_roles,
        document_id=document_id,
        base_version_number=base_version_number,
        content=content,
        content_type="application/pdf",
        reason=reason,
        region=region,
    )


def _base_lock_cmd(
    *,
    document_id: UUID,
    tenant_id: UUID = ACME_TENANT,
    actor_id: UUID = SUPERVISOR_USER,
    actor_roles: tuple[str, ...] = ("supervisor",),
) -> LockDocumentCommand:
    return LockDocumentCommand(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_roles=actor_roles,
        document_id=document_id,
    )


# ---------------------------------------------------------------------------
# 1. Redaction happy path — preserves original, appends new version.
# ---------------------------------------------------------------------------


async def test_redact_document__happy_path__preserves_original_and_audits(
    clock: FixedClock,
) -> None:
    """A redaction:

    - writes the redacted bytes through ``DocumentStore.put`` exactly once;
    - appends a ``DocumentRedaction`` row with ``base_version_number`` = 1;
    - does NOT modify the original ``Document`` aggregate (sha256, size,
      current_version, locked all unchanged);
    - appends a ``DocumentRedacted`` audit row + outbox row in the same
      transaction.
    """
    world = _seeded_world()
    document = world["document"]
    initial_version = world["initial_version"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, initial_version)
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_redact_cmd(document_id=document.id)
    result = await redact(
        cmd,
        document_repo=doc_repo,
        document_store=storage,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # Bytes landed in storage exactly once.
    assert len(storage.puts) == 1
    put = storage.puts[0]
    assert put.tenant_id == ACME_TENANT
    assert put.content == cmd.content
    assert put.sha256 == hashlib.sha256(cmd.content).hexdigest()

    # The redaction row carries the base version + reason + region.
    assert len(doc_repo.redactions) == 1
    redaction = doc_repo.redactions[0]
    assert isinstance(redaction, DocumentRedaction)
    assert redaction.document_id == document.id
    assert redaction.tenant_id == ACME_TENANT
    assert redaction.base_version_number == 1
    assert redaction.reason == "ssn redacted"
    assert redaction.region == "page 2 paragraph 3"
    assert redaction.status == "ACTIVE"
    assert redaction.created_by == SUPERVISOR_USER

    # Original bytes are still retrievable from the original DocumentVersion row.
    history = await doc_repo.list_versions(ACME_TENANT, document.id)
    assert [v.version_number for v in history] == [1]
    assert history[0].sha256 == initial_version.sha256
    assert history[0].size == initial_version.size

    # The Document aggregate's current sha/size/current_version point
    # at the original — the redaction does NOT mutate the parent.
    current_doc = await doc_repo.get(ACME_TENANT, document.id)
    assert current_doc is not None
    assert current_doc.sha256 == initial_version.sha256
    assert current_doc.size == initial_version.size
    assert current_doc.current_version == 1
    assert current_doc.locked is False

    # Audit row: DocumentRedacted, in tenant, with subject = document,
    # case_id populated, payload includes the original sha/size so the
    # chain can prove "redacted from this original".
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.event_type == REDACTION_CREATED
    assert event.tenant_id == ACME_TENANT
    assert event.subject_id == document.id
    assert event.case_id == world["case"].id
    assert event.payload["original_sha256"] == initial_version.sha256
    assert event.payload["original_size"] == initial_version.size
    assert event.payload["base_version_number"] == 1
    assert event.payload["reason"] == "ssn redacted"

    # Outbox row + return value.
    assert len(outbox.events) == 1
    assert outbox.events[0].event_type == REDACTION_CREATED
    assert outbox.events[0].payload["document_id"] == str(document.id)
    assert result.redaction.id == redaction.id
    assert result.document.id == document.id


# ---------------------------------------------------------------------------
# 2. Prior ACTIVE redaction is superseded.
# ---------------------------------------------------------------------------


async def test_redact_document__prior_active_redaction__superseded(
    clock: FixedClock,
) -> None:
    """A second redaction marks the first one SUPERSEDED in the same
    transaction so the timeline always shows exactly one ACTIVE row
    per document at any time (P03-US6 — "versioned redactions
    preserving original").
    """
    world = _seeded_world()
    document = world["document"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, world["initial_version"])
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd1 = _base_redact_cmd(document_id=document.id, reason="redaction pass 1")
    await redact(
        cmd1,
        document_repo=doc_repo,
        document_store=storage,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )
    cmd2 = _base_redact_cmd(document_id=document.id, reason="redaction pass 2")
    await redact(
        cmd2,
        document_repo=doc_repo,
        document_store=storage,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # Two rows total: the first is SUPERSEDED, the second is ACTIVE.
    bucket = doc_repo._redactions[(ACME_TENANT, document.id)]  # noqa: SLF001
    assert len(bucket) == 2
    statuses = sorted([r.status for r in bucket])
    assert statuses == ["ACTIVE", "SUPERSEDED"]
    superseded = next(r for r in bucket if r.status == "SUPERSEDED")
    assert superseded.superseded_at == FIXED_NOW
    assert superseded.superseded_by == SUPERVISOR_USER


# ---------------------------------------------------------------------------
# 3. Locked document refuses redactions (raises DocumentLocked).
# ---------------------------------------------------------------------------


async def test_redact_document__locked_document__raises_document_locked(
    clock: FixedClock,
) -> None:
    """A document with ``locked=True`` refuses the redaction with a
    ``DocumentLocked`` exception; no bytes are stored and no audit row
    is appended. The lock guard is symmetric with
    ``Document.with_new_version``.
    """
    world = _seeded_world(locked=True)
    document = world["document"]
    assert document.locked is True

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, world["initial_version"])
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_redact_cmd(document_id=document.id)
    with pytest.raises(DocumentLocked):
        await redact(
            cmd,
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    # No side effects: no bytes stored, no redaction row, no audit row,
    # no outbox row. The lock refusal is the contract.
    assert storage.puts == []
    assert doc_repo.redactions == []
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 4. Lock happy path.
# ---------------------------------------------------------------------------


async def test_lock_document__happy_path__sets_flags_and_audits(
    clock: FixedClock,
) -> None:
    """Locking the document:

    - flips ``locked`` / ``locked_at`` / ``locked_by`` on the aggregate;
    - persists the locked state via ``save_locked``;
    - appends a ``DocumentLocked`` audit row + outbox row in the same
      transaction.
    """
    world = _seeded_world()
    document = world["document"]

    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, world["initial_version"])
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_lock_cmd(document_id=document.id)
    result = await lock(
        cmd,
        document_repo=doc_repo,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # The aggregate reflects the lock.
    assert result.document.locked is True
    assert result.document.locked_at == FIXED_NOW
    assert result.document.locked_by == SUPERVISOR_USER
    assert result.document.id == document.id

    # The repository recorded it.
    assert len(doc_repo.locked) == 1
    assert doc_repo.locked[0].locked is True

    # Audit row + outbox row.
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.event_type == DOCUMENT_LOCKED
    assert event.tenant_id == ACME_TENANT
    assert event.subject_id == document.id
    assert event.case_id == world["case"].id
    assert event.payload["locked_by"] == str(SUPERVISOR_USER)

    assert len(outbox.events) == 1
    assert outbox.events[0].event_type == DOCUMENT_LOCKED


# ---------------------------------------------------------------------------
# 5. Locked document rejects edits — subsequent version add raises.
# ---------------------------------------------------------------------------


async def test_lock_document__subsequent_version__raises_document_locked(
    clock: FixedClock,
) -> None:
    """After locking, an attempt to add a new version (P03-US2) raises
    ``DocumentLocked`` from the domain (``Document.with_new_version``)
    BEFORE any bytes are stored. This is the integration between
    P03-US2 and P03-US8 — the contract is "a locked document refuses
    further mutations".
    """
    world = _seeded_world(locked=True)
    document = world["document"]
    initial_version = world["initial_version"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, initial_version)
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = VersionDocumentCommand(
        tenant_id=ACME_TENANT,
        actor_id=REVIEWER_USER,
        actor_roles=("reviewer",),
        document_id=document.id,
        content=b"%PDF-1.4\nv2 bytes",
        content_type="application/pdf",
    )
    with pytest.raises(DocumentLocked):
        await add_version(
            cmd,
            case_repo=world["case_repo"],
            case_type_version_repo=world["version_repo"],
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    # No bytes stored, no version row, no audit row.
    assert storage.puts == []
    history = await doc_repo.list_versions(ACME_TENANT, document.id)
    assert [v.version_number for v in history] == [1]


# ---------------------------------------------------------------------------
# 6. Re-locking raises DocumentLocked.
# ---------------------------------------------------------------------------


async def test_lock_document__already_locked__raises_document_locked(
    clock: FixedClock,
) -> None:
    """Re-locking a locked document raises ``DocumentLocked`` so a
    misconfigured retry cannot silently re-lock. The contract is the
    second attempt is a refused event, not a no-op.
    """
    world = _seeded_world(locked=True)
    document = world["document"]

    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, world["initial_version"])
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_lock_cmd(document_id=document.id)
    with pytest.raises(DocumentLocked):
        await lock(
            cmd,
            document_repo=doc_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    # No new audit / outbox rows — the refusal is the contract.
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 7. Authorization — intake role is denied document.redact / document.lock.
# ---------------------------------------------------------------------------


async def test_redact_document__unauthorized_actor__raises_forbidden(
    clock: FixedClock,
) -> None:
    """An actor without ``document.redact`` (e.g. the ``intake`` role)
    is forbidden. No bytes stored, no audit row.
    """
    world = _seeded_world()
    document = world["document"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, world["initial_version"])
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_redact_cmd(document_id=document.id, actor_id=INTAKE_USER, actor_roles=("intake",))
    with pytest.raises(Forbidden):
        await redact(
            cmd,
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    assert storage.puts == []
    assert doc_repo.redactions == []
    assert audit.events == []


async def test_lock_document__unauthorized_actor__raises_forbidden(
    clock: FixedClock,
) -> None:
    """An actor without ``document.lock`` is forbidden."""
    world = _seeded_world()
    document = world["document"]

    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, world["initial_version"])
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_lock_cmd(document_id=document.id, actor_id=INTAKE_USER, actor_roles=("intake",))
    with pytest.raises(Forbidden):
        await lock(
            cmd,
            document_repo=doc_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    assert doc_repo.locked == []
    assert audit.events == []


# ---------------------------------------------------------------------------
# 8. Cross-tenant document id surfaces as NotFound.
# ---------------------------------------------------------------------------


async def test_redact_document__cross_tenant_document__raises_not_found(
    clock: FixedClock,
) -> None:
    """A redaction with ``tenant_id = OTHER_TENANT`` against a document
    that belongs to ``ACME_TENANT`` surfaces as ``NotFound`` so the
    API never confirms cross-tenant existence (Constitution V).
    """
    world = _seeded_world()
    document = world["document"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, world["initial_version"])
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_redact_cmd(document_id=document.id, tenant_id=OTHER_TENANT)
    with pytest.raises(NotFound):
        await redact(
            cmd,
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    assert storage.puts == []
    assert doc_repo.redactions == []
    assert audit.events == []


async def test_lock_document__cross_tenant_document__raises_not_found(
    clock: FixedClock,
) -> None:
    """Cross-tenant lock attempt surfaces as ``NotFound``."""
    world = _seeded_world()
    document = world["document"]

    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, world["initial_version"])
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_lock_cmd(document_id=document.id, tenant_id=OTHER_TENANT)
    with pytest.raises(NotFound):
        await lock(
            cmd,
            document_repo=doc_repo,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    assert doc_repo.locked == []
    assert audit.events == []


# ---------------------------------------------------------------------------
# 9. Storage failure raises DependencyUnavailable with no side effects.
# ---------------------------------------------------------------------------


async def test_redact_document__storage_failure__rolls_back_without_audit(
    clock: FixedClock,
) -> None:
    """When ``DocumentStore.put`` raises ``DependencyUnavailable``, the
    use case propagates the exception. The HTTP layer (out of scope
    here) is responsible for rolling the transaction back; the test
    asserts no audit / outbox rows were appended in this case.
    """
    world = _seeded_world()
    document = world["document"]

    storage = FailingDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, world["initial_version"])
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_redact_cmd(document_id=document.id)
    with pytest.raises(DependencyUnavailable):
        await redact(
            cmd,
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )

    assert doc_repo.redactions == []
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 10. Invalid base_version_number is rejected.
# ---------------------------------------------------------------------------


async def test_redact_document__invalid_base_version__raises_validation_failed(
    clock: FixedClock,
) -> None:
    """``base_version_number`` outside the document's history
    (1..current_version) raises ``ValidationFailed`` with field set.
    """
    world = _seeded_world()
    document = world["document"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, world["initial_version"])
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_redact_cmd(document_id=document.id, base_version_number=999)
    with pytest.raises(ValidationFailed) as exc_info:
        await redact(
            cmd,
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    assert exc_info.value.field == "base_version_number"

    assert storage.puts == []
    assert doc_repo.redactions == []
    assert audit.events == []


# ---------------------------------------------------------------------------
# 11. Empty reason is rejected.
# ---------------------------------------------------------------------------


async def test_redact_document__empty_reason__raises_validation_failed(
    clock: FixedClock,
) -> None:
    """An empty reason string raises ``ValidationFailed`` with field
    set. Reason is the only free text on the row so the audit can prove
    why the redaction was made.
    """
    world = _seeded_world()
    document = world["document"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_doc_repo(doc_repo, document, world["initial_version"])
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_redact_cmd(document_id=document.id, reason="")
    with pytest.raises(ValidationFailed) as exc_info:
        await redact(
            cmd,
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    assert exc_info.value.field == "reason"

    assert storage.puts == []
    assert doc_repo.redactions == []


__all__ = [
    "protect_document",
]
