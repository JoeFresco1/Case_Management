"""Tests for the evidence bundle use case (P14-US8 / FM-060).

Acceptance criteria from the task:

1. The bundle contains the case aggregate, every document attached
   to the case, and the audit history.
2. Sensitive case fields are redacted (mirrors the timeline
   allowlist + the case-type schema's sensitive-field keys).
3. The bundle is portable: a ZIP archive with a manifest that
   carries the SHA-256 of every entry, so a downstream tool can
   verify integrity off-system.
4. An unauthorized case is refused with ``Forbidden`` (HTTP 403).
5. A missing / cross-tenant case surfaces as ``NotFound``
   (HTTP 404) so a forbidden actor cannot probe for existence
   (Constitution V).

The use case is tested in-memory with the same fakes the case +
document + audit tests use. The bundle is opened with the standard
``zipfile`` module so the on-disk shape is verified end-to-end.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.authorization import (
    ROLE_INTAKE,
    ROLE_SUPERVISOR,
)
from app.application.exports import evidence_bundle as evidence_bundle_uc
from app.application.exports.evidence_bundle import (
    BUNDLE_SCHEMA_VERSION,
    BuildEvidenceBundleCommand,
    EvidenceBundle,
)
from app.domain.audit import (
    AUDIT_EXPORTED,
    CASE_CREATED,
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
    DocumentSensitivity,
    DocumentVersion,
)
from app.domain.errors import Forbidden, NotFound, ValidationFailed

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ACTOR_ADMIN = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ACTOR_INTAKE = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
ACTOR_REVIEWER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
CORRELATION_BASE = "corr-evidence-bundle"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _StagedAudit:
    tenant_id: UUID
    event_type: str
    actor_id: UUID
    actor_type: str
    subject_type: str
    subject_id: UUID
    case_id: UUID | None
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]
    occurred_at: datetime


class InMemoryAuditStore:
    """In-memory ``AuditEventStore`` for the bundle tests.

    Implements ``append`` and ``list_for_case``. The list returns the
    rows in insertion order with a deterministic chain: each row's
    ``hash`` is computed from the previous row's hash + the canonical
    payload, so a downstream auditor can replay the chain with
    :func:`app.domain.audit.verify_chain`.
    """

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.events: list[_StagedAudit] = []
        self._chain: list[dict[str, Any]] = []

    async def append(
        self,
        *,
        tenant_id: UUID,
        event_type: str,
        actor_id: UUID,
        actor_type: str,
        subject_type: str,
        subject_id: UUID,
        case_id: UUID | None,
        correlation_id: str,
        causation_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        prev_hash = self._chain[-1]["hash"] if self._chain else "0" * 64
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256((prev_hash + "\x00" + canonical).encode("utf-8")).hexdigest()
        row_id = uuid4()
        self._chain.append(
            {
                "id": row_id,
                "hash": digest,
                "prev_hash": prev_hash,
                "payload": payload,
            }
        )
        self.events.append(
            _StagedAudit(
                tenant_id=tenant_id,
                event_type=event_type,
                actor_id=actor_id,
                actor_type=actor_type,
                subject_type=subject_type,
                subject_id=subject_id,
                case_id=case_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        )
        # Stash the row id on the staged record for downstream lookups.
        self.events[-1].__dict__["row_id"] = row_id  # type: ignore[attr-defined]

    async def list_for_case(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID,
        event_types=None,
        actor_id: UUID | None = None,
        since=None,
        until=None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[Any]:
        if tenant_id != self.tenant_id:
            return []
        out: list[Any] = []
        for staged, chain in zip(self.events, self._chain, strict=True):
            if staged.tenant_id != tenant_id:
                continue
            if staged.case_id != case_id:
                continue
            if event_types and staged.event_type not in event_types:
                continue
            if actor_id is not None and staged.actor_id != actor_id:
                continue
            out.append(_AuditRow(staged, chain))
        return out[:limit]

    async def list_for_export(
        self,
        *,
        tenant_id: UUID,
        case_id: UUID | None,
        since=None,
        until=None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[Any]:
        return await self.list_for_case(
            tenant_id=tenant_id,
            case_id=case_id,  # type: ignore[arg-type]
            limit=limit,
        )


@dataclass
class _AuditRow:
    """The shape :func:`_audit_row_to_dict` reads from.

    Mirrors the SQL adapter's row shape so the bundle's audit
    section is exactly the same wire shape the durable store
    produces.
    """

    staged: _StagedAudit
    chain: dict[str, Any]

    @property
    def id(self) -> UUID:
        return self.chain["id"]

    @property
    def event_type(self) -> str:
        return self.staged.event_type

    @property
    def actor_id(self) -> UUID:
        return self.staged.actor_id

    @property
    def actor_type(self) -> str:
        return self.staged.actor_type

    @property
    def subject_type(self) -> str:
        return self.staged.subject_type

    @property
    def subject_id(self) -> UUID:
        return self.staged.subject_id

    @property
    def correlation_id(self) -> str:
        return self.staged.correlation_id

    @property
    def causation_id(self) -> str | None:
        return self.staged.causation_id

    @property
    def payload(self) -> dict[str, Any]:
        return self.staged.payload

    @property
    def occurred_at(self) -> datetime:
        return self.staged.occurred_at

    @property
    def hash(self) -> str:
        return self.chain["hash"]

    @property
    def prev_hash(self) -> str:
        return self.chain["prev_hash"]


@dataclass
class _StoredDocument:
    document: Document
    version: DocumentVersion
    content: bytes


class InMemoryDocumentRepository:
    """Minimal ``DocumentRepository`` for the bundle tests.

    Implements ``list_for_case`` and ``get``; the bundle use case
    only needs the former. The fake stores the document content
    alongside the metadata so a test can verify the bytes that
    land in the archive.
    """

    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id
        self._documents: list[_StoredDocument] = []

    def add(
        self,
        document: Document,
        version: DocumentVersion,
    ) -> None:
        self._documents.append(_StoredDocument(document=document, version=version, content=b""))

    async def list_for_case(self, *, tenant_id: UUID, case_id: UUID) -> list[Document]:
        return [
            row.document
            for row in self._documents
            if row.document.tenant_id == tenant_id and row.document.case_id == case_id
        ]

    async def get(self, tenant_id: UUID, document_id: UUID) -> Document | None:
        for row in self._documents:
            if row.document.tenant_id == tenant_id and row.document.id == document_id:
                return row.document
        return None

    def content_for(self, document_id: UUID) -> bytes | None:
        for row in self._documents:
            if row.document.id == document_id:
                return row.content
        return None


class InMemoryDocumentStore:
    """In-memory ``DocumentStore`` keyed on ``content_ref``.

    The production ``DocumentStore.get`` accepts a ``content_ref``;
    the test fake keeps a dict keyed on the same value so the
    bundle use case can read the bytes the way the production
    adapter would.
    """

    def __init__(self) -> None:
        self._by_ref: dict[str, bytes] = {}

    def put(
        self,
        *,
        content: bytes,
        content_ref: str,
    ) -> None:
        self._by_ref[content_ref] = content

    async def put_async(
        self,
        *,
        tenant_id: UUID,
        content: bytes,
        content_type: str,
        sha256: str,
    ) -> str:
        content_ref = f"in-memory://{uuid4()}"
        self._by_ref[content_ref] = content
        return content_ref

    async def get(self, *, tenant_id: UUID, content_ref: str) -> bytes:
        if content_ref not in self._by_ref:
            raise KeyError(f"unknown content_ref: {content_ref}")
        return self._by_ref[content_ref]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_case_type(
    *,
    tenant_id: UUID,
    fields: tuple[CaseTypeField, ...],
) -> tuple[CaseType, CaseTypeVersion]:
    """Build a published CaseType + CaseTypeVersion with the given fields."""
    version = CaseTypeVersion(
        id=uuid4(),
        tenant_id=tenant_id,
        case_type_id=uuid4(),
        version_number=1,
        schema=CaseTypeSchema(fields=fields),
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        published_by=uuid4(),
    )
    case_type = CaseType(
        id=version.case_type_id,
        tenant_id=tenant_id,
        key="ServiceRequest",
        name="Service Request",
        description="",
        current_version_id=version.id,
    )
    return case_type, version


def _make_case(
    *,
    tenant_id: UUID,
    case_type_version: CaseTypeVersion,
    data: dict[str, Any] | None = None,
) -> Case:
    return Case.create(
        tenant_id=tenant_id,
        reference=CaseReference(f"C-TEST-{datetime.now(tz=UTC).year}-000001"),
        case_type_version=case_type_version,
        title="Service request 1",
        description="",
        data=data or {"title": "x", "priority": "normal", "ssn": "123-45-6789"},
        priority="normal",
        created_by=ACTOR_ADMIN,
        now=datetime(2026, 8, 31, tzinfo=UTC),
        sensitivity="internal",
    )


def _make_document(
    *,
    tenant_id: UUID,
    case_id: UUID,
    file_name: str,
    content: bytes,
    classification: str = "Evidence",
) -> tuple[Document, DocumentVersion]:
    sha = hashlib.sha256(content).hexdigest()
    document = Document.create(
        tenant_id=tenant_id,
        case_id=case_id,
        file_name=file_name,
        content_type="application/octet-stream",
        classification=classification,
        sensitivity=DocumentSensitivity.INTERNAL,
        sha256=sha,
        size=len(content),
        storage_ref=f"in-memory://{file_name}",
        uploaded_by=ACTOR_ADMIN,
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    version = DocumentVersion(
        id=uuid4(),
        tenant_id=tenant_id,
        document_id=document.id,
        version_number=1,
        sha256=sha,
        size=len(content),
        storage_ref=document.storage_ref,
        content_type=document.content_type,
        uploaded_by=ACTOR_ADMIN,
        uploaded_at=datetime(2026, 8, 31, tzinfo=UTC),
    )
    return document, version


def _fixed_clock() -> Any:
    class _Clock:
        def now(self) -> datetime:
            return datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

    return _Clock()


def _dedupe() -> Any:
    """In-memory dedupe that always says "first time"."""

    class _Dedupe:
        def __init__(self) -> None:
            self._seen: set[tuple[UUID, str, str, str]] = set()

        def __call__(
            self, tenant_id: UUID, event_type: str, correlation_id: str, payload_digest: str
        ) -> bool:
            key = (tenant_id, event_type, correlation_id, payload_digest)
            if key in self._seen:
                return True
            self._seen.add(key)
            return False

    return _Dedupe()


def _open_bundle(bundle: EvidenceBundle) -> dict[str, Any]:
    """Open the bundle's ZIP and return the entries as a name->bytes dict."""
    out: dict[str, Any] = {}
    with zipfile.ZipFile(io.BytesIO(bundle.archive), "r") as zf:
        for name in zf.namelist():
            out[name] = zf.read(name)
    return out


def _verify_entry_hashes(entries: dict[str, Any]) -> None:
    """Confirm every entry's bytes hash to the value listed in the manifest.

    This is the integrity contract a downstream tool relies on; the
    test asserts the bundle's manifest is self-consistent.
    """
    manifest = json.loads(entries["manifest.json"].decode("utf-8"))
    for entry in manifest["entries"]:
        name = entry["name"]
        assert name in entries, f"manifest references {name!r} but archive is missing it"
        body = entries[name]
        assert len(body) == entry["size"], (
            f"size mismatch for {name}: manifest={entry['size']} actual={len(body)}"
        )
        digest = hashlib.sha256(body).hexdigest()
        assert digest == entry["sha256"], (
            f"hash mismatch for {name}: manifest={entry['sha256']} actual={digest}"
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Harness:
    case_repo: InMemoryCaseRepository
    case_type_version_repo: InMemoryCaseTypeVersionRepository
    document_repo: InMemoryDocumentRepository
    document_store: InMemoryDocumentStore
    audit: InMemoryAuditStore

    async def build(self, cmd: BuildEvidenceBundleCommand) -> EvidenceBundle:
        return await evidence_bundle_uc.handle(
            cmd,
            case_repo=self.case_repo,
            case_type_version_repo=self.case_type_version_repo,
            document_repo=self.document_repo,
            document_store=self.document_store,
            audit=self.audit,
            dedupe=_dedupe(),
            clock=_fixed_clock(),
        )


# --- in-memory repositories reused from the case + documents fakes -----


class InMemoryCaseRepository:
    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id
        self._cases: list[Case] = []

    def add(self, case: Case) -> None:
        self._cases.append(case)

    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
        for c in self._cases:
            if c.tenant_id == tenant_id and c.id == case_id:
                return c
        return None

    async def update(self, case: Case) -> None:
        return None

    async def add_async(self, case: Case) -> None:
        self._cases.append(case)


class InMemoryCaseTypeVersionRepository:
    def __init__(self) -> None:
        self._by_id: dict[UUID, CaseTypeVersion] = {}

    def add(self, version: CaseTypeVersion) -> None:
        self._by_id[version.id] = version

    async def get(self, tenant_id: UUID, case_type_version_id: UUID) -> CaseTypeVersion | None:
        v = self._by_id.get(case_type_version_id)
        if v is None or v.tenant_id != tenant_id:
            return None
        return v

    async def get_published_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None:
        for v in self._by_id.values():
            if (
                v.tenant_id == tenant_id
                and v.case_type_id == case_type_id
                and v.status is CaseTypeVersionStatus.PUBLISHED
            ):
                return v
        return None


@pytest.fixture
def harness() -> _Harness:
    case_repo = InMemoryCaseRepository(TENANT_A)
    case_type_version_repo = InMemoryCaseTypeVersionRepository()
    document_repo = InMemoryDocumentRepository(TENANT_A)
    document_store = InMemoryDocumentStore()
    audit = InMemoryAuditStore(TENANT_A)
    return _Harness(
        case_repo=case_repo,  # type: ignore[arg-type]
        case_type_version_repo=case_type_version_repo,  # type: ignore[arg-type]
        document_repo=document_repo,
        document_store=document_store,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
    )


@pytest.fixture
def sensitive_fields() -> tuple[CaseTypeField, ...]:
    """A schema with one restricted field so the redactor has work to do."""
    return (
        CaseTypeField(key="title", label="Title", type="text", required=True),
        CaseTypeField(key="priority", label="Priority", type="text", required=True),
        CaseTypeField(
            key="ssn",
            label="Social security number",
            type="text",
            required=True,
            sensitivity="restricted",
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_build_evidence_bundle__authorized_case__produces_portable_archive_with_redactions(
    harness: _Harness, sensitive_fields: tuple[CaseTypeField, ...]
) -> None:
    """The bundle ships case + documents + audit, restricted fields are redacted.

    Acceptance criterion 1 + 2: the archive contains the case
    aggregate, every document attached to the case, and the audit
    history. The case's ``ssn`` field is redacted to the standard
    marker; the audit payload is redacted through the same
    allowlist the timeline uses.
    """
    case_type, version = _make_case_type(tenant_id=TENANT_A, fields=sensitive_fields)
    harness.case_type_version_repo.add(version)
    case = _make_case(tenant_id=TENANT_A, case_type_version=version)
    harness.case_repo.add(case)

    # Attach two documents so the archive has more than one file.
    doc1, ver1 = _make_document(
        tenant_id=TENANT_A,
        case_id=case.id,
        file_name="evidence.txt",
        content=b"redacted evidence bytes 1",
    )
    doc2, ver2 = _make_document(
        tenant_id=TENANT_A,
        case_id=case.id,
        file_name="contract.txt",
        content=b"contract bytes",
    )
    harness.document_repo.add(doc1, ver1)
    harness.document_repo.add(doc2, ver2)
    # The bundle use case reads the bytes via the DocumentStore; the
    # store keys on the document's ``storage_ref`` so the fake mirrors
    # the production flow.
    harness.document_store._by_ref[doc1.storage_ref] = b"redacted evidence bytes 1"  # type: ignore[attr-defined]
    harness.document_store._by_ref[doc2.storage_ref] = b"contract bytes"  # type: ignore[attr-defined]

    # Pre-populate the audit chain with two events so the bundle's
    # audit section has real content to verify.
    base_time = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
    await harness.audit.append(
        tenant_id=TENANT_A,
        event_type=CASE_CREATED,
        actor_id=ACTOR_ADMIN,
        actor_type="user",
        subject_type="case",
        subject_id=case.id,
        case_id=case.id,
        correlation_id="corr-1",
        causation_id=None,
        payload={
            "case_reference": case.reference.value,
            "ssn": "should-be-redacted",
            "case_type_version_id": str(version.id),
        },
        occurred_at=base_time,
    )
    await harness.audit.append(
        tenant_id=TENANT_A,
        event_type="DocumentAdded",
        actor_id=ACTOR_ADMIN,
        actor_type="user",
        subject_type="document",
        subject_id=doc1.id,
        case_id=case.id,
        correlation_id="corr-2",
        causation_id=None,
        payload={
            "document_id": str(doc1.id),
            "case_id": str(case.id),
            "file_name": doc1.file_name,
        },
        occurred_at=base_time,
    )

    cmd = BuildEvidenceBundleCommand(
        tenant_id=TENANT_A,
        actor_id=ACTOR_ADMIN,
        actor_roles=ROLE_SUPERVISOR.actions and ("supervisor",),  # has case.read + document.read
        case_id=case.id,
        reason="legal-hold-2026-09",
        can_see_sensitive=False,
    )
    bundle = await harness.build(cmd)

    # 1. Archive is a real ZIP.
    entries = _open_bundle(bundle)
    assert set(entries) >= {
        "manifest.json",
        "case.json",
        "audit.json",
        f"documents/{doc1.id}/metadata.json",
        f"documents/{doc1.id}/content.bin",
        f"documents/{doc2.id}/metadata.json",
        f"documents/{doc2.id}/content.bin",
    }

    # 2. Document bytes round-trip unchanged.
    assert entries[f"documents/{doc1.id}/content.bin"] == b"redacted evidence bytes 1"
    assert entries[f"documents/{doc2.id}/content.bin"] == b"contract bytes"

    # 3. Manifest integrity contract: every entry's bytes hash to
    # the value listed in the manifest.
    _verify_entry_hashes(entries)
    manifest = json.loads(entries["manifest.json"].decode("utf-8"))
    assert manifest["schema_version"] == BUNDLE_SCHEMA_VERSION
    assert manifest["case"]["id"] == str(case.id)
    assert manifest["case"]["reference"] == case.reference.value
    assert manifest["reason"] == "legal-hold-2026-09"
    assert manifest["document_count"] == 2
    assert manifest["audit_row_count"] >= 2
    assert bundle.manifest_hash == hashlib.sha256(entries["manifest.json"]).hexdigest()

    # 4. Case data is redacted: the ``ssn`` field is replaced
    # with the standard marker; the case reference + non-sensitive
    # fields pass through untouched.
    case_dict = json.loads(entries["case.json"].decode("utf-8"))
    assert case_dict["redacted_fields"] == ["ssn"]
    assert case_dict["data"]["ssn"] == {"redacted": True, "reason": "sensitive"}
    assert case_dict["data"]["title"] == "x"
    assert case_dict["data"]["priority"] == "normal"

    # 5. Audit payload redaction mirrors the timeline allowlist.
    audit_dict = json.loads(entries["audit.json"].decode("utf-8"))
    assert audit_dict["case_id"] == str(case.id)
    rows = audit_dict["rows"]
    assert len(rows) == 2
    case_created = next(r for r in rows if r["event_type"] == CASE_CREATED)
    assert case_created["payload"]["ssn"] == "***"
    assert case_created["payload"]["case_reference"] == case.reference.value
    doc_added = next(r for r in rows if r["event_type"] == "DocumentAdded")
    assert doc_added["payload"]["document_id"] == str(doc1.id)
    # The timeline allowlist does not include ``file_name``; the
    # redaction replaces it with the standard marker so a
    # leak-by-payload cannot surface the on-disk filename.
    assert doc_added["payload"]["file_name"] == "***"

    # 6. The export itself is an audit event so the action is
    # visible on the chain.
    assert any(
        e.event_type == AUDIT_EXPORTED and e.case_id == case.id for e in harness.audit.events
    )
    exported = next(
        e for e in harness.audit.events if e.event_type == AUDIT_EXPORTED and e.case_id == case.id
    )
    assert exported.payload["bundle"] is True
    assert exported.payload["manifest_hash"] == bundle.manifest_hash
    assert exported.payload["document_count"] == 2


async def test_build_evidence_bundle__unauthorized_actor__raises_forbidden(
    harness: _Harness, sensitive_fields: tuple[CaseTypeField, ...]
) -> None:
    """A reviewer without `case.read` on the case is refused with ``Forbidden``.

    Acceptance criterion 4: an unauthorized case surfaces as
    ``Forbidden`` (HTTP 403 at the boundary). The bundle is not
    built; no audit row is appended; the case aggregate is not
    read.
    """
    case_type, version = _make_case_type(tenant_id=TENANT_A, fields=sensitive_fields)
    harness.case_type_version_repo.add(version)
    case = _make_case(tenant_id=TENANT_A, case_type_version=version)
    harness.case_repo.add(case)

    cmd = BuildEvidenceBundleCommand(
        tenant_id=TENANT_A,
        actor_id=ACTOR_INTAKE,
        actor_roles=ROLE_INTAKE.actions and ("intake",),
        case_id=case.id,
        reason="legal-hold",
    )
    with pytest.raises(Forbidden):
        await harness.build(cmd)

    # The case is not in the audit chain — a denied export must
    # not leak a row.
    assert not any(
        e.event_type == AUDIT_EXPORTED and e.case_id == case.id for e in harness.audit.events
    )


async def test_build_evidence_bundle__missing_case__raises_not_found(
    harness: _Harness, sensitive_fields: tuple[CaseTypeField, ...]
) -> None:
    """A missing case surfaces as ``NotFound``.

    Acceptance criterion 5: a forbidden actor must not be able to
    probe for case existence (Constitution V). The use case raises
    ``NotFound`` for any case the repository does not return for
    the actor's tenant; the HTTP layer maps that to 404.
    """
    cmd = BuildEvidenceBundleCommand(
        tenant_id=TENANT_A,
        actor_id=ACTOR_ADMIN,
        actor_roles=("supervisor",),
        case_id=uuid4(),
        reason="legal-hold",
    )
    with pytest.raises(NotFound):
        await harness.build(cmd)


async def test_build_evidence_bundle__cross_tenant_case__raises_not_found(
    harness: _Harness, sensitive_fields: tuple[CaseTypeField, ...]
) -> None:
    """A case in another tenant surfaces as ``NotFound``.

    The repository's tenant filter prevents a cross-tenant probe
    from surfacing the case; the use case raises ``NotFound`` so
    the API does not confirm cross-tenant existence.
    """
    # Build a case in tenant B.
    case_type_b, version_b = _make_case_type(tenant_id=TENANT_B, fields=sensitive_fields)
    other_case_repo = InMemoryCaseRepository(TENANT_B)
    other_case_repo.add(_make_case(tenant_id=TENANT_B, case_type_version=version_b))

    cmd = BuildEvidenceBundleCommand(
        tenant_id=TENANT_A,  # actor is in tenant A
        actor_id=ACTOR_ADMIN,
        actor_roles=("supervisor",),
        case_id=other_case_repo._cases[0].id,  # case belongs to tenant B
        reason="legal-hold",
    )
    with pytest.raises(NotFound):
        await harness.build(cmd)


async def test_build_evidence_bundle__empty_reason__raises_validation_failed(
    harness: _Harness, sensitive_fields: tuple[CaseTypeField, ...]
) -> None:
    """An empty reason is refused with a structured ``ValidationFailed``.

    The reason lands on the audit chain verbatim; refusing an
    empty reason keeps the audit trail honest.
    """
    case_type, version = _make_case_type(tenant_id=TENANT_A, fields=sensitive_fields)
    harness.case_type_version_repo.add(version)
    case = _make_case(tenant_id=TENANT_A, case_type_version=version)
    harness.case_repo.add(case)

    cmd = BuildEvidenceBundleCommand(
        tenant_id=TENANT_A,
        actor_id=ACTOR_ADMIN,
        actor_roles=("supervisor",),
        case_id=case.id,
        reason="   ",  # whitespace-only
    )
    with pytest.raises(ValidationFailed) as exc_info:
        await harness.build(cmd)
    assert exc_info.value.field == "reason"


async def test_build_evidence_bundle__no_documents__still_produces_valid_archive(
    harness: _Harness, sensitive_fields: tuple[CaseTypeField, ...]
) -> None:
    """A case with no documents still produces a verifiable bundle.

    The manifest's ``document_count`` is zero; the archive has no
    ``documents/`` entries; the integrity contract still holds.
    """
    case_type, version = _make_case_type(tenant_id=TENANT_A, fields=sensitive_fields)
    harness.case_type_version_repo.add(version)
    case = _make_case(tenant_id=TENANT_A, case_type_version=version)
    harness.case_repo.add(case)

    cmd = BuildEvidenceBundleCommand(
        tenant_id=TENANT_A,
        actor_id=ACTOR_ADMIN,
        actor_roles=("supervisor",),
        case_id=case.id,
        reason="legal-hold",
    )
    bundle = await harness.build(cmd)
    entries = _open_bundle(bundle)
    assert "manifest.json" in entries
    assert "case.json" in entries
    assert "audit.json" in entries
    assert not any(name.startswith("documents/") for name in entries)
    _verify_entry_hashes(entries)


__all__ = [
    "test_build_evidence_bundle__authorized_case__produces_portable_archive_with_redactions",
    "test_build_evidence_bundle__unauthorized_actor__raises_forbidden",
    "test_build_evidence_bundle__missing_case__raises_not_found",
    "test_build_evidence_bundle__cross_tenant_case__raises_not_found",
    "test_build_evidence_bundle__empty_reason__raises_validation_failed",
    "test_build_evidence_bundle__no_documents__still_produces_valid_archive",
]
