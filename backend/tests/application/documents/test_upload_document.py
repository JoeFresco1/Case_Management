"""Tests for ``app.application.documents.upload_document.handle`` (P03-US1 / FM-022 / FR-005).

Acceptance scenarios from ``specs/001-case-management-platform/first-slice/spec.md``
US4 / FR-005:

1. **Success**: an authorized upload within the case-type policy
   persists the bytes through the storage adapter, records a
   metadata row, a version row, a ``DocumentAdded`` audit row, and a
   ``DocumentAdded`` outbox row — all in the same transaction. The
   content hash is recorded.
2. **Disallowed type**: an upload with a content type outside the
   case type's policy raises ``ValidationFailed`` and writes nothing.
3. **Oversize**: an upload exceeding the case type's max size raises
   ``ValidationFailed`` and writes nothing.
4. **Storage unavailable**: when the storage adapter raises
   ``DependencyUnavailable`` mid-upload, the use case propagates the
   failure and writes no metadata / version / audit / outbox rows —
   the caller's transaction rollback is the boundary that keeps
   partial state out of the database.

Naming follows ``test_<unit>_<scenario>__<expectation>``
(AGENTS.md §4.R5). Tests are deterministic: a fixed clock, fixed UUIDs.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.documents.upload_document import (
    DOCUMENT_ADDED,
    DOCUMENT_REJECTED,
    UploadDocumentCommand,
    handle,
    record_rejection,
)
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.document import DocumentSensitivity
from app.domain.errors import (
    DependencyUnavailable,
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

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


def _make_case_type(
    *,
    tenant_id: UUID,
    allowed_content_types: tuple[str, ...] = ("application/pdf",),
    max_size_bytes: int | None = 1024,  # 1 KiB ceiling for tests
    classifications: tuple[str, ...] = ("Contract", "Correspondence", "Evidence"),
    reject_duplicates: bool = False,
) -> tuple[CaseType, CaseTypeVersion]:
    """Build a published CaseType whose policy governs the upload tests."""
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="title",
                label="Title",
                type="text",
                required=True,
            ),
        ),
        document_classifications=classifications,
        rules=(),
        document_max_size_bytes=max_size_bytes,
        document_allowed_content_types=allowed_content_types,
        document_reject_duplicates=reject_duplicates,
    )
    version = CaseTypeVersion(
        id=uuid4(),
        tenant_id=tenant_id,
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
        tenant_id=tenant_id,
        key="ServiceRequest",
        name="Service Request",
        description="",
        sensitivity_default="internal",
        current_version_id=version.id,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    return case_type, version


@pytest.fixture
def seeded_world(
    tmp_path: Any,
) -> dict[str, Any]:
    """Build a tenant + published case type + persisted case + storage fake.

    The returned dict holds the in-memory repositories and stores so
    each test can poke at them directly. ``tmp_path`` is unused but
    keeps the fixture signature compatible with future filesystem
    tests.
    """
    _ = tmp_path

    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()
    case_type, version = _make_case_type(tenant_id=ACME_TENANT)
    case_type_repo.add(case_type)
    version_repo.add(version)

    # Persist a case in the initial state so the upload has something
    # to attach to.
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
    # Ensure the in-memory repo holds the case.
    case_repo._cases.append(case)  # noqa: SLF001 — test-only reach-in

    return {
        "case_repo": case_repo,
        "case_type_repo": case_type_repo,
        "version_repo": version_repo,
        "case_type": case_type,
        "version": version,
        "case": case,
    }


def _base_cmd(
    *,
    case_id: UUID | None = None,
    tenant_id: UUID = ACME_TENANT,
    actor_id: UUID = REVIEWER_USER,
    actor_roles: tuple[str, ...] = ("reviewer",),
    content_type: str = "application/pdf",
    classification: str = "Contract",
    sensitivity: DocumentSensitivity = DocumentSensitivity.INTERNAL,
    content: bytes = b"%PDF-1.4\nhello",
) -> UploadDocumentCommand:
    """Build a default upload command for the happy path.

    The default content is a 12-byte PDF-like header. Use overrides
    to exercise the policy failure paths.
    """
    return UploadDocumentCommand(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_roles=actor_roles,
        case_id=case_id or uuid4(),
        file_name="onboarding.pdf",
        content_type=content_type,
        classification=classification,
        sensitivity=sensitivity,
        content=content,
    )


# ---------------------------------------------------------------------------
# 1. Success
# ---------------------------------------------------------------------------


async def test_upload_document__happy_path__persists_bytes_metadata_audit_outbox(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    """A valid upload stores bytes through the adapter, stages one
    Document row, one DocumentVersion row, one ``DocumentAdded`` audit
    event, and one ``DocumentAdded`` outbox event — all in the same
    transaction.
    """
    case = seeded_world["case"]
    cmd = _base_cmd(case_id=case.id)

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    result = await handle(
        cmd,
        case_repo=seeded_world["case_repo"],
        case_type_version_repo=seeded_world["version_repo"],
        document_repo=doc_repo,
        document_store=storage,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    # Bytes landed at the adapter exactly once.
    assert len(storage.puts) == 1
    put = storage.puts[0]
    assert put.tenant_id == ACME_TENANT
    assert put.content == cmd.content
    assert put.sha256 == hashlib.sha256(cmd.content).hexdigest()
    assert put.storage_ref.endswith(f"/{put.sha256[:2]}/{put.sha256}")

    # Document metadata + initial version staged exactly once.
    assert len(doc_repo.added) == 1
    document, version = doc_repo.added[0]
    assert document.tenant_id == ACME_TENANT
    assert document.case_id == case.id
    assert document.content_type == cmd.content_type
    assert document.classification == cmd.classification
    assert document.sensitivity is DocumentSensitivity.INTERNAL
    assert document.sha256 == put.sha256
    assert document.size == len(cmd.content)
    assert document.current_version == 1
    assert document.uploaded_by == REVIEWER_USER
    assert document.storage_ref == put.storage_ref
    assert version.document_id == document.id
    assert version.version_number == 1
    assert version.sha256 == document.sha256
    assert result.document.id == document.id
    assert result.version.id == version.id

    # Audit row: DocumentAdded in the same tenant.
    assert len(audit.events) == 1
    audit_event = audit.events[0]
    assert audit_event.tenant_id == ACME_TENANT
    assert audit_event.event_type == DOCUMENT_ADDED
    assert audit_event.subject_id == document.id
    assert audit_event.case_id == case.id
    assert audit_event.payload["sha256"] == document.sha256
    assert audit_event.payload["size"] == len(cmd.content)
    assert audit_event.payload["classification"] == "Contract"

    # Outbox row: same tenant + same event type.
    assert len(outbox.events) == 1
    outbox_event = outbox.events[0]
    assert outbox_event.tenant_id == ACME_TENANT
    assert outbox_event.event_type == DOCUMENT_ADDED
    assert outbox_event.payload["document_id"] == str(document.id)
    assert outbox_event.payload["sha256"] == document.sha256


# ---------------------------------------------------------------------------
# 2. Disallowed content type
# ---------------------------------------------------------------------------


async def test_upload_document__disallowed_content_type__rejected_with_no_writes(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    """An upload whose content type is not in the case type's
    ``document_allowed_content_types`` is rejected with
    ``ValidationFailed`` and no metadata, version, audit, or outbox
    rows are written. The router is responsible for appending the
    ``DocumentRejected`` audit event when this exception escapes.
    """
    case = seeded_world["case"]
    cmd = _base_cmd(case_id=case.id, content_type="application/x-executable")

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    with pytest.raises(ValidationFailed) as excinfo:
        await handle(
            cmd,
            case_repo=seeded_world["case_repo"],
            case_type_version_repo=seeded_world["version_repo"],
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    assert excinfo.value.field == "content_type"
    assert "content type" in str(excinfo.value).lower()

    # Nothing was written anywhere.
    assert storage.puts == []
    assert doc_repo.added == []
    assert audit.events == []
    assert outbox.events == []

    # The HTTP layer follows up with a DocumentRejected audit; the use
    # case itself does not write one. Simulate the HTTP follow-up to
    # confirm the contract: the rejection row is durable and safe.
    await record_rejection(
        cmd,
        audit=audit,
        clock=clock,
        reason="content_type",
        details={"message": str(excinfo.value)},
    )
    assert len(audit.events) == 1
    rejection = audit.events[0]
    assert rejection.event_type == DOCUMENT_REJECTED
    assert rejection.payload["reason"] == "content_type"


# ---------------------------------------------------------------------------
# 3. Oversize
# ---------------------------------------------------------------------------


async def test_upload_document__oversize__rejected_with_no_writes(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    """An upload exceeding the case type's ``document_max_size_bytes``
    is rejected with ``ValidationFailed`` and writes nothing.
    """
    case = seeded_world["case"]
    # The fixture's default max is 1024 bytes.
    too_big = b"x" * (1024 + 1)
    cmd = _base_cmd(case_id=case.id, content=too_big)

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    with pytest.raises(ValidationFailed) as excinfo:
        await handle(
            cmd,
            case_repo=seeded_world["case_repo"],
            case_type_version_repo=seeded_world["version_repo"],
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    assert excinfo.value.field == "size"
    assert "max size" in str(excinfo.value).lower()

    assert storage.puts == []
    assert doc_repo.added == []
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 4. Storage unavailable
# ---------------------------------------------------------------------------


async def test_upload_document__storage_unavailable__rolls_back_no_writes(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    """When ``DocumentStore.put`` raises ``DependencyUnavailable``, the
    use case propagates the failure. No metadata, version, audit, or
    outbox rows are produced — the caller is expected to roll the
    transaction back so partial state never lands.
    """
    case = seeded_world["case"]
    cmd = _base_cmd(case_id=case.id)

    storage = FailingDocumentStore(message="disk full")
    doc_repo = InMemoryDocumentRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    with pytest.raises(DependencyUnavailable) as excinfo:
        await handle(
            cmd,
            case_repo=seeded_world["case_repo"],
            case_type_version_repo=seeded_world["version_repo"],
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    assert "disk full" in str(excinfo.value)

    # The failing store saw the put attempt; the application-side
    # stores saw nothing. This is the boundary that keeps partial
    # state out of the database: a real deployment rolls back the
    # transaction here and the audit/outbox/doc rows never flush.
    assert len(storage.puts) == 1
    assert doc_repo.added == []
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# Bonus: authorization denial (DocumentUpload requires a role that grants it).
# ---------------------------------------------------------------------------


async def test_upload_document__actor_without_document_upload_role__forbidden(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    """The ``intake`` role lacks ``document.upload``; the policy denies
    the request before any storage write is attempted.
    """
    case = seeded_world["case"]
    cmd = _base_cmd(
        case_id=case.id,
        actor_id=INTAKE_USER,
        actor_roles=("intake",),
    )

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    with pytest.raises(Forbidden):
        await handle(
            cmd,
            case_repo=seeded_world["case_repo"],
            case_type_version_repo=seeded_world["version_repo"],
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    assert storage.puts == []
    assert doc_repo.added == []
    assert audit.events == []
    assert outbox.events == []


async def test_upload_document__unknown_case__raises_not_found(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    """A case id that does not exist within the actor's tenant raises
    ``NotFound``. Cross-tenant case ids are indistinguishable from
    non-existent ids (Constitution V — no existence leak).
    """
    cmd = _base_cmd(case_id=uuid4())

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    with pytest.raises(NotFound):
        await handle(
            cmd,
            case_repo=seeded_world["case_repo"],
            case_type_version_repo=seeded_world["version_repo"],
            document_repo=doc_repo,
            document_store=storage,
            audit=audit,
            outbox=outbox,
            clock=clock,
        )
    assert storage.puts == []
    assert doc_repo.added == []
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# Duplicate rejection when configured (T055)
# ---------------------------------------------------------------------------


def _seeded_reject_world() -> dict[str, Any]:
    """World whose case type opts into duplicate rejection."""
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")
    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()
    case_type, version = _make_case_type(tenant_id=ACME_TENANT, reject_duplicates=True)
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
    case_repo._cases.append(case)  # noqa: SLF001 — test-only reach-in
    return {
        "case_repo": case_repo,
        "version_repo": version_repo,
        "case": case,
    }


async def _upload_once(
    world: dict[str, Any],
    *,
    content: bytes,
    clock: FixedClock,
    storage: Any,
    doc_repo: Any,
) -> Any:
    """Run one upload against the given fakes; return the result."""
    return await handle(
        _base_cmd(case_id=world["case"].id, content=content),
        case_repo=world["case_repo"],
        case_type_version_repo=world["version_repo"],
        document_repo=doc_repo,
        document_store=storage,
        audit=InMemoryAuditStore(),
        outbox=InMemoryOutbox(),
        clock=clock,
    )


async def test_upload_document__duplicate_configured__rejects_same_bytes(
    clock: FixedClock,
) -> None:
    """Same bytes twice on one case fails when the type opts in."""
    # // given — one successful upload of these exact bytes.
    world = _seeded_reject_world()
    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    content = b"%PDF-1.4\nhello"
    await _upload_once(world, content=content, clock=clock, storage=storage, doc_repo=doc_repo)
    puts_before = len(storage.puts)

    # // when — identical bytes again.
    with pytest.raises(ValidationFailed, match="duplicate"):
        await _upload_once(world, content=content, clock=clock, storage=storage, doc_repo=doc_repo)

    # // then — rejected before storing bytes or staging metadata.
    assert len(storage.puts) == puts_before
    assert len(doc_repo.added) == 1


async def test_upload_document__duplicate_not_configured__allows_same_bytes(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    """Default policy (flag off) keeps the legacy allow-duplicates path."""
    # // given
    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    content = b"%PDF-1.4\nhello"

    # // when — same bytes twice.
    first = await _upload_once(
        seeded_world, content=content, clock=clock, storage=storage, doc_repo=doc_repo
    )
    second = await _upload_once(
        seeded_world, content=content, clock=clock, storage=storage, doc_repo=doc_repo
    )

    # // then — both land as separate documents.
    assert second.document.id != first.document.id
    assert len(doc_repo.added) == 2


async def test_upload_document__duplicate_configured__different_bytes_pass(
    clock: FixedClock,
) -> None:
    """The flag rejects identical digests, not the case itself."""
    # // given
    world = _seeded_reject_world()
    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    first = await _upload_once(
        world, content=b"%PDF-1.4\nhello", clock=clock, storage=storage, doc_repo=doc_repo
    )

    # // when — different bytes.
    second = await _upload_once(
        world, content=b"%PDF-1.4\nworld", clock=clock, storage=storage, doc_repo=doc_repo
    )

    # // then
    assert second.document.sha256 != first.document.sha256
    assert len(doc_repo.added) == 2
