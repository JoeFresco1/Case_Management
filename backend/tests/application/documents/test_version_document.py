"""Tests for ``app.application.documents.version_document.handle`` (P03-US2 / FM-023 / FR-006).

Acceptance scenarios from ``pillar.md §6``:

- **C5**: A new version of a document is uploaded. The prior version
  remains retrievable; the current version pointer updates; a
  ``VersionAdded`` event is emitted.
- The version add runs in the same transaction as the audit + outbox
  events.
- Tenant isolation: a cross-tenant document id is not visible.

Naming follows ``test_<unit>_<scenario>__<expectation>``
(AGENTS.md §4.R5). Tests are deterministic: a fixed clock, fixed UUIDs.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.documents.upload_document import DOCUMENT_ADDED
from app.application.documents.version_document import (
    VERSION_ADDED,
    VersionDocumentCommand,
    handle,
)
from app.domain.case import Case, CaseReference
from app.domain.case_type import (
    CaseType,
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.document import Document, DocumentSensitivity, DocumentVersion
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
    max_size_bytes: int | None = 4096,  # 4 KiB ceiling for tests
) -> tuple[CaseType, CaseTypeVersion]:
    """Build a published CaseType whose policy governs the add-version tests."""
    schema = CaseTypeSchema(
        fields=(
            CaseTypeField(
                key="title",
                label="Title",
                type="text",
                required=True,
            ),
        ),
        document_classifications=("Contract", "Correspondence", "Evidence"),
        rules=(),
        document_max_size_bytes=max_size_bytes,
        document_allowed_content_types=allowed_content_types,
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
def seeded_world() -> dict[str, Any]:
    """Build a tenant + published case type + persisted case + initial document.

    Returns the in-memory repositories plus the v1 Document so each
    test can poke at them directly. The case is created with the same
    shape as the upload tests so the version flow exercises the same
    policy path.
    """
    case_repo = InMemoryCaseRepository()
    case_repo.set_tenant_short(ACME_TENANT, "ACME")

    case_type_repo = InMemoryCaseTypeRepository()
    version_repo = InMemoryCaseTypeVersionRepository()
    case_type, version = _make_case_type(tenant_id=ACME_TENANT)
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

    # The initial v1 document + DocumentVersion row, as if the upload
    # use case had already run.
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
        "case_type": case_type,
        "version": version,
        "case": case,
        "document": document,
        "initial_version": initial_version,
        "initial_content": initial_content,
    }


def _seed_document_repo(
    doc_repo: InMemoryDocumentRepository,
    document: Document,
    initial_version: DocumentVersion,
) -> None:
    """Stage the initial v1 (Document, DocumentVersion) on the in-memory repo."""
    # The fake's ``add_document_and_version`` is async — but we
    # synchronously need to populate the internal dict so that ``get``
    # + ``list_versions`` resolve. Reach in to the test-only fields.
    doc_repo.added.append((document, initial_version))
    doc_repo._docs[(document.tenant_id, document.id)] = document  # noqa: SLF001
    doc_repo._versions.setdefault(  # noqa: SLF001
        (document.tenant_id, document.id), []
    ).append(initial_version)


def _base_cmd(
    *,
    document_id: UUID,
    tenant_id: UUID = ACME_TENANT,
    actor_id: UUID = REVIEWER_USER,
    actor_roles: tuple[str, ...] = ("reviewer",),
    content_type: str = "application/pdf",
    content: bytes = b"%PDF-1.4\nv2 bytes",
) -> VersionDocumentCommand:
    """Build a default add-version command for the happy path."""
    return VersionDocumentCommand(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_roles=actor_roles,
        document_id=document_id,
        content=content,
        content_type=content_type,
    )


# ---------------------------------------------------------------------------
# 1. Happy path — preserves prior content, bumps current_version, audits.
# ---------------------------------------------------------------------------


async def test_version_document__happy_path__preserves_history_and_audits(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    """Adding a version stages a new DocumentVersion row, bumps
    ``current_version``, preserves the prior row, writes a
    ``VersionAdded`` audit row, and emits a ``VersionAdded`` outbox
    event — all in the same transaction.
    """
    document = seeded_world["document"]
    case = seeded_world["case"]
    initial_version = seeded_world["initial_version"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_document_repo(doc_repo, document, initial_version)
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_cmd(document_id=document.id)
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

    # The new bytes landed in storage exactly once.
    assert len(storage.puts) == 1
    put = storage.puts[0]
    assert put.tenant_id == ACME_TENANT
    assert put.content == cmd.content
    assert put.sha256 == hashlib.sha256(cmd.content).hexdigest()

    # The repository staged exactly one version add.
    assert len(doc_repo.versioned) == 1
    next_document, new_version = doc_repo.versioned[0]

    # Prior version is preserved — the version-history query still
    # returns the v1 row alongside the new v2 row.
    history = await doc_repo.list_versions(ACME_TENANT, document.id)
    assert [v.version_number for v in history] == [1, 2]
    assert history[0].sha256 == initial_version.sha256
    assert history[1].sha256 == new_version.sha256

    # The metadata aggregate was updated: current_version bumped, the
    # active sha/size/ref reflect the new bytes, and the v1 metadata
    # was NOT overwritten with v2 bytes.
    assert next_document.current_version == 2
    assert next_document.sha256 == new_version.sha256
    assert next_document.size == new_version.size
    assert next_document.storage_ref == new_version.storage_ref
    assert next_document.updated_at == FIXED_NOW

    # The new version row carries the same id as the aggregate's
    # current_version pointer — a reader using the metadata
    # ``current_version`` value can locate the row by id.
    assert result.document.id == next_document.id
    assert result.version.id == new_version.id
    assert result.version.version_number == 2
    assert result.version.uploaded_by == REVIEWER_USER

    # Audit row: VersionAdded, in the same tenant, with the case + the
    # document as subjects.
    assert len(audit.events) == 1
    audit_event = audit.events[0]
    assert audit_event.tenant_id == ACME_TENANT
    assert audit_event.event_type == VERSION_ADDED
    assert audit_event.subject_id == document.id
    assert audit_event.case_id == case.id
    assert audit_event.payload["new_version_number"] == 2
    assert audit_event.payload["previous_version_number"] == 1
    assert audit_event.payload["sha256"] == new_version.sha256
    assert audit_event.payload["classification"] == "Contract"

    # Outbox row: same tenant + same event type + version pointer.
    assert len(outbox.events) == 1
    outbox_event = outbox.events[0]
    assert outbox_event.tenant_id == ACME_TENANT
    assert outbox_event.event_type == VERSION_ADDED
    assert outbox_event.payload["document_id"] == str(document.id)
    assert outbox_event.payload["version_number"] == 2
    assert outbox_event.payload["sha256"] == new_version.sha256


# ---------------------------------------------------------------------------
# 2. Multiple sequential versions preserve all history.
# ---------------------------------------------------------------------------


async def test_version_document__multiple_versions__history_grows_monotonically(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    """Two consecutive add-version calls produce three total rows
    (v1 + v2 + v3) in ascending order. The metadata's current_version
    pointer tracks the latest.
    """
    document = seeded_world["document"]
    initial_version = seeded_world["initial_version"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_document_repo(doc_repo, document, initial_version)
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd_v2 = _base_cmd(document_id=document.id, content=b"%PDF-1.4\nv2 bytes")
    await handle(
        cmd_v2,
        case_repo=seeded_world["case_repo"],
        case_type_version_repo=seeded_world["version_repo"],
        document_repo=doc_repo,
        document_store=storage,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    cmd_v3 = _base_cmd(document_id=document.id, content=b"%PDF-1.4\nv3 bytes")
    await handle(
        cmd_v3,
        case_repo=seeded_world["case_repo"],
        case_type_version_repo=seeded_world["version_repo"],
        document_repo=doc_repo,
        document_store=storage,
        audit=audit,
        outbox=outbox,
        clock=clock,
    )

    history = await doc_repo.list_versions(ACME_TENANT, document.id)
    assert [v.version_number for v in history] == [1, 2, 3]
    assert history[0].sha256 == initial_version.sha256
    assert history[1].sha256 == hashlib.sha256(b"%PDF-1.4\nv2 bytes").hexdigest()
    assert history[2].sha256 == hashlib.sha256(b"%PDF-1.4\nv3 bytes").hexdigest()

    # Final metadata state reflects v3.
    final_doc = await doc_repo.get(ACME_TENANT, document.id)
    assert final_doc is not None
    assert final_doc.current_version == 3
    assert final_doc.sha256 == history[2].sha256

    # Two audit rows + two outbox rows — the VersionAdded event is the
    # only one the version use case emits.
    assert len(audit.events) == 2
    assert all(e.event_type == VERSION_ADDED for e in audit.events)
    assert len(outbox.events) == 2


# ---------------------------------------------------------------------------
# 3. Disallowed content type — rejected with no writes.
# ---------------------------------------------------------------------------


async def test_version_document__disallowed_content_type__rejected_with_no_writes(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    document = seeded_world["document"]
    initial_version = seeded_world["initial_version"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_document_repo(doc_repo, document, initial_version)
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_cmd(document_id=document.id, content_type="application/x-executable")

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

    assert storage.puts == []
    assert doc_repo.versioned == []
    assert audit.events == []
    assert outbox.events == []

    # History is unchanged — only the original v1 row remains.
    history = await doc_repo.list_versions(ACME_TENANT, document.id)
    assert [v.version_number for v in history] == [1]


# ---------------------------------------------------------------------------
# 4. Oversize — rejected with no writes.
# ---------------------------------------------------------------------------


async def test_version_document__oversize__rejected_with_no_writes(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    document = seeded_world["document"]
    initial_version = seeded_world["initial_version"]

    too_big = b"x" * (4096 + 1)
    cmd = _base_cmd(document_id=document.id, content=too_big)

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_document_repo(doc_repo, document, initial_version)
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
    assert doc_repo.versioned == []
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 5. Storage unavailable — propagates DependencyUnavailable, no writes.
# ---------------------------------------------------------------------------


async def test_version_document__storage_unavailable__rolls_back_no_writes(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    document = seeded_world["document"]
    initial_version = seeded_world["initial_version"]

    storage = FailingDocumentStore(message="disk full")
    doc_repo = InMemoryDocumentRepository()
    _seed_document_repo(doc_repo, document, initial_version)
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_cmd(document_id=document.id)
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

    # The storage adapter saw the put attempt; nothing else wrote.
    assert len(storage.puts) == 1
    assert doc_repo.versioned == []
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 6. Authorization — actor without document.upload is forbidden.
# ---------------------------------------------------------------------------


async def test_version_document__actor_without_document_upload_role__forbidden(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    document = seeded_world["document"]
    initial_version = seeded_world["initial_version"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_document_repo(doc_repo, document, initial_version)
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_cmd(
        document_id=document.id,
        actor_id=INTAKE_USER,
        actor_roles=("intake",),
    )

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
    assert doc_repo.versioned == []
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 7. Unknown document id — NotFound.
# ---------------------------------------------------------------------------


async def test_version_document__unknown_document__raises_not_found(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_cmd(document_id=uuid4())
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
    assert doc_repo.versioned == []
    assert audit.events == []
    assert outbox.events == []


# ---------------------------------------------------------------------------
# 8. Tenant isolation — cross-tenant document id is not visible.
# ---------------------------------------------------------------------------


async def test_version_document__cross_tenant_document__not_found(
    seeded_world: dict[str, Any],
    clock: FixedClock,
) -> None:
    """A document that exists in tenant A is invisible to a request
    carrying tenant B's id. The repository filter is the tenant
    boundary; the use case surfaces NotFound rather than leaking the
    cross-tenant id's existence (Constitution V).
    """
    document = seeded_world["document"]
    initial_version = seeded_world["initial_version"]

    storage = InMemoryDocumentStore()
    doc_repo = InMemoryDocumentRepository()
    _seed_document_repo(doc_repo, document, initial_version)
    audit = InMemoryAuditStore()
    outbox = InMemoryOutbox()

    cmd = _base_cmd(document_id=document.id, tenant_id=OTHER_TENANT)
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

    # No cross-tenant writes were attempted.
    assert storage.puts == []
    assert doc_repo.versioned == []
    assert audit.events == []
    assert outbox.events == []
    # The tenant A state is preserved unchanged.
    history = await doc_repo.list_versions(ACME_TENANT, document.id)
    assert [v.version_number for v in history] == [1]


# ---------------------------------------------------------------------------
# 9. Event-type isolation — VersionAdded does not collapse to DocumentAdded.
# ---------------------------------------------------------------------------


async def test_version_document__event_type_is_version_added_not_document_added() -> None:
    """``VERSION_ADDED`` is a distinct literal from ``DOCUMENT_ADDED``
    so subscribers can route them independently. A typo here would
    silently re-route the event.
    """
    assert VERSION_ADDED != DOCUMENT_ADDED
    assert VERSION_ADDED == "VersionAdded"
    assert DOCUMENT_ADDED == "DocumentAdded"
