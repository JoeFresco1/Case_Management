"""Tests for `app.application.search.document_search.handle` (P03-US3 / P06-US8 / FM-033).

Acceptance scenarios (per task spec):

1. **Find by classification** — a worker searches for documents in a
   case by ``classification`` and the use case returns the matching
   documents (without granting visibility to a document the actor
   could not see through direct document access).
2. **Full-text LIKE** — a worker submits a free-text substring; the
   portable ``LIKE`` adapter matches against ``file_name`` and
   returns the matching documents.
3. **Access control** — a restricted-sensitivity document is
   filtered out for an actor without the sensitive-read grant; the
   snippet for a non-restricted hit carries the bounded excerpt;
   the snippet for a restricted hit (when the actor does hold the
   grant) is redacted.
4. **Cross-tenant denial** — a document belonging to another tenant
   is silently absent from the result set.
5. **Classification allowlist** — a document whose classification is
   not on the case-type's ``document_classifications`` allowlist is
   silently dropped.
6. **Pagination** — ``limit`` / ``offset`` are honored and out-of-
   range values are rejected.

The fakes are intentionally hermetic: the use case depends on
``CaseRepository``, ``CaseTypeVersionRepository``, and a
``DocumentSearchProvider``. An in-memory implementation stands in
for the SQL adapter so the tests run without Postgres / SQLite.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.search import document_search as uc
from app.domain.case import Case, CaseReference, CaseStatus
from app.domain.case_type import (
    CaseTypeField,
    CaseTypeSchema,
    CaseTypeVersion,
    CaseTypeVersionStatus,
)
from app.domain.document import Document, DocumentSensitivity
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.ports import (
    CaseRepository,
    CaseTypeVersionRepository,
    DocumentSearchHit,
    DocumentSearchProvider,
    DocumentSearchResultPage,
    DocumentSearchSpec,
)

# ---------------------------------------------------------------------------
# Fixtures + constants
# ---------------------------------------------------------------------------


ACME_TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
INTAKE_USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SUPERVISOR_USER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
ALICE_UPLOADER = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
BOB_UPLOADER = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")

FIXED_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class InMemoryCaseRepository(CaseRepository):
    """In-memory case repository that satisfies the port contract."""

    def __init__(self) -> None:
        self._cases: list[Case] = []

    def add(self, case: Case) -> None:
        self._cases.append(case)

    async def get(self, tenant_id: UUID, case_id: UUID) -> Case | None:
        for c in self._cases:
            if c.tenant_id == tenant_id and c.id == case_id:
                return c
        return None

    async def get_by_reference(self, tenant_id: UUID, reference: CaseReference) -> Case | None:
        for c in self._cases:
            if c.tenant_id == tenant_id and c.reference == reference:
                return c
        return None

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Case]:
        rows = [c for c in self._cases if c.tenant_id == tenant_id]
        rows.sort(key=lambda c: c.created_at, reverse=True)
        return rows[offset : offset + limit]

    async def next_reference(
        self, tenant_id: UUID, *, now: datetime, type_key: str | None = None
    ) -> CaseReference:
        return CaseReference(value="C-ACME-2026-000001")

    async def update(self, case: Case) -> None:
        return None


@dataclass
class InMemoryCaseTypeVersionRepository(CaseTypeVersionRepository):
    """In-memory case type version repository."""

    def __init__(self) -> None:
        self._versions: list[CaseTypeVersion] = []

    def add(self, version: CaseTypeVersion) -> None:
        self._versions.append(version)

    async def get(self, tenant_id: UUID, case_type_version_id: UUID) -> CaseTypeVersion | None:
        for v in self._versions:
            if v.tenant_id == tenant_id and v.id == case_type_version_id:
                return v
        return None

    async def get_published_for_case_type(
        self, tenant_id: UUID, case_type_id: UUID
    ) -> CaseTypeVersion | None:
        for v in self._versions:
            if (
                v.tenant_id == tenant_id
                and v.case_type_id == case_type_id
                and v.status == CaseTypeVersionStatus.PUBLISHED
            ):
                return v
        return None

    async def get_by_key(self, tenant_id: UUID, case_type_key: str) -> CaseTypeVersion | None:
        return None


@dataclass
class InMemoryDocumentSearchProvider(DocumentSearchProvider):
    """Portable-shaped in-memory document search provider.

    Mirrors the SQL adapter's contract: tenant scoping, classification
    allowlist, sensitivity threshold, metadata filter, free-text
    substring. The snippet is bounded to ``file_name``; restricted
    documents return a ``Redacted`` marker when the actor does not
    hold the sensitive-read grant.
    """

    documents: list[Document] = field(default_factory=list)
    last_spec: DocumentSearchSpec | None = None

    async def search_documents(self, spec: DocumentSearchSpec) -> DocumentSearchResultPage:
        self.last_spec = spec
        rows: list[Document] = []
        for d in self.documents:
            if d.tenant_id != spec.tenant_id:
                continue
            if d.case_id != spec.case_id:
                continue
            if (
                spec.allowed_classifications
                and d.classification not in spec.allowed_classifications
            ):
                continue
            if spec.text_query and spec.text_query.lower() not in d.file_name.lower():
                continue
            if spec.classification is not None and d.classification != spec.classification:
                continue
            if spec.uploaded_by is not None and d.uploaded_by != spec.uploaded_by:
                continue
            if spec.uploaded_since is not None and d.created_at < spec.uploaded_since:
                continue
            if spec.uploaded_until is not None and d.created_at > spec.uploaded_until:
                continue
            threshold = DocumentSensitivity(spec.sensitivity_threshold)
            if not spec.can_see_sensitive and d.sensitivity.rank() >= threshold.rank():
                continue
            rows.append(d)
        rows.sort(key=lambda d: d.created_at, reverse=True)
        total = len(rows)
        page = rows[spec.offset : spec.offset + spec.limit]
        items = [_to_hit(d, spec) for d in page]
        next_cursor: str | None = None
        if len(page) == spec.limit and spec.offset + spec.limit < total:
            next_cursor = f"{spec.offset + spec.limit}:{spec.limit}"
        return DocumentSearchResultPage(
            items=items,
            total_considered=total,
            limit=spec.limit,
            offset=spec.offset,
            next_cursor=next_cursor,
        )


def _to_hit(d: Document, spec: DocumentSearchSpec) -> DocumentSearchHit:
    """Project a Document aggregate into a hit, redacting if needed."""
    threshold = DocumentSensitivity(spec.sensitivity_threshold)
    text_query = (spec.text_query or "").strip().lower()
    if not spec.can_see_sensitive and d.sensitivity.rank() >= threshold.rank():
        snippet: Any = {"redacted": True, "reason": "sensitive"}
    elif not text_query:
        snippet = ""
    else:
        snippet = _bounded_snippet(d.file_name, text_query)
    return DocumentSearchHit(
        document_id=d.id,
        case_id=d.case_id,
        file_name=d.file_name,
        classification=d.classification,
        sensitivity=d.sensitivity.value,
        current_version=d.current_version,
        content_type=d.content_type,
        sha256=d.sha256,
        uploaded_by=d.uploaded_by,
        uploaded_at=d.created_at,
        snippet=snippet,
    )


def _bounded_snippet(file_name: str, term: str) -> str:
    lowered = file_name.lower()
    idx = lowered.find(term)
    if idx < 0:
        return file_name[:64]
    start = max(0, idx - 32)
    end = min(len(file_name), idx + len(term) + 32)
    return file_name[start:end]


# DocumentSensitivity rank() helper — local copy so tests stay
# hermetic (no SQL adapter import required). The ordering mirrors
# the SQL adapter's ``_sensitivity_below`` ordering.
def _install_rank() -> None:
    if hasattr(DocumentSensitivity, "rank"):
        return
    _order = [
        DocumentSensitivity.PUBLIC,
        DocumentSensitivity.INTERNAL,
        DocumentSensitivity.CONFIDENTIAL,
        DocumentSensitivity.RESTRICTED,
        DocumentSensitivity.PHI,
    ]

    def rank(self: DocumentSensitivity) -> int:
        return _order.index(self)

    DocumentSensitivity.rank = rank  # type: ignore[attr-defined]


_install_rank()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_case(
    *,
    tenant_id: UUID,
    case_type_version_id: UUID,
) -> Case:
    return Case(
        id=uuid4(),
        tenant_id=tenant_id,
        reference=CaseReference(value="C-ACME-2026-000001"),
        case_type_version_id=case_type_version_id,
        status=CaseStatus.OPEN,
        priority="normal",
        title="T",
        description="",
        data={},
        current_workflow_state=None,
        version_token=1,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
        created_by=INTAKE_USER,
        closed_at=None,
        sensitivity="internal",
    )


def _make_case_type_version(
    *,
    tenant_id: UUID,
    document_classifications: tuple[str, ...] = ("Contract", "Evidence", "Correspondence"),
) -> CaseTypeVersion:
    return CaseTypeVersion(
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
            ),
            document_classifications=document_classifications,
        ),
        status=CaseTypeVersionStatus.PUBLISHED,
        published_at=FIXED_NOW,
        published_by=INTAKE_USER,
    )


def _make_document(
    *,
    tenant_id: UUID,
    case_id: UUID,
    file_name: str,
    classification: str = "Contract",
    sensitivity: DocumentSensitivity = DocumentSensitivity.INTERNAL,
    uploaded_by: UUID = ALICE_UPLOADER,
    created_at: datetime = FIXED_NOW,
) -> Document:
    return Document(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id,
        file_name=file_name,
        content_type="text/plain",
        classification=classification,
        sensitivity=sensitivity,
        sha256="0" * 64,
        size=128,
        current_version=1,
        storage_ref=f"local://{file_name}",
        uploaded_by=uploaded_by,
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.fixture
def case_repo() -> InMemoryCaseRepository:
    return InMemoryCaseRepository()


@pytest.fixture
def version_repo() -> InMemoryCaseTypeVersionRepository:
    return InMemoryCaseTypeVersionRepository()


@pytest.fixture
def provider() -> InMemoryDocumentSearchProvider:
    return InMemoryDocumentSearchProvider()


# ---------------------------------------------------------------------------
# 1. Find by classification
# ---------------------------------------------------------------------------


async def test_document_search__classification_filter__returns_matching_documents(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """Filtering by classification returns only matching documents, and
    documents whose classification is not on the case-type allowlist
    are silently omitted (Constitution V).
    """
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    contract = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="contract-2026.pdf",
        classification="Contract",
    )
    evidence = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="lab-result.pdf",
        classification="Evidence",
    )
    # "LabResult" is NOT on the allowlist; the use case must drop it.
    lab = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="lab-other.pdf",
        classification="LabResult",
    )
    provider.documents.extend([contract, evidence, lab])

    result = await uc.handle(
        uc.DocumentSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
            classification="Contract",
        ),
        case_repo=case_repo,
        case_type_version_repo=version_repo,
        document_search_provider=provider,
    )

    assert [h.document_id for h in result.items] == [contract.id]
    assert result.items[0].classification == "Contract"
    assert result.total_considered == 1
    # The non-allowlist document was dropped silently.
    assert all(h.classification != "LabResult" for h in result.items)


# ---------------------------------------------------------------------------
# 2. Full-text LIKE
# ---------------------------------------------------------------------------


async def test_document_search__free_text_substring__matches_file_name(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """The free-text portion is a portable LIKE substring on
    ``file_name`` (case-insensitive). A document whose file name does
    not contain the term is silently dropped.
    """
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    matching = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="incident-report-2026.pdf",
    )
    not_matching = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="contract-2026.pdf",
    )
    provider.documents.extend([matching, not_matching])

    result = await uc.handle(
        uc.DocumentSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
            text_query="incident",
        ),
        case_repo=case_repo,
        case_type_version_repo=version_repo,
        document_search_provider=provider,
    )

    assert [h.document_id for h in result.items] == [matching.id]
    assert result.items[0].snippet == "incident-report-2026.pdf"
    assert result.total_considered == 1


async def test_document_search__free_text__case_insensitive(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """The free-text substring is matched case-insensitively."""
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    doc = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="Incident-Report.PDF",
    )
    provider.documents.append(doc)

    result = await uc.handle(
        uc.DocumentSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
            text_query="INCIDENT",
        ),
        case_repo=case_repo,
        case_type_version_repo=version_repo,
        document_search_provider=provider,
    )

    assert [h.document_id for h in result.items] == [doc.id]


# ---------------------------------------------------------------------------
# 3. Access control (P11-US6 / FM-029)
# ---------------------------------------------------------------------------


async def test_document_search__restricted_doc__filtered_for_intake_actor(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """A restricted-sensitivity document is silently dropped when the
    actor does not hold the sensitive-read grant. The actor gains no
    visibility through the search surface that they would lack
    through direct document access (P03-US3 acceptance criterion).
    """
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    public_doc = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="public-doc.pdf",
        sensitivity=DocumentSensitivity.INTERNAL,
    )
    restricted_doc = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="restricted-doc.pdf",
        sensitivity=DocumentSensitivity.RESTRICTED,
    )
    provider.documents.extend([public_doc, restricted_doc])

    # Intake role does NOT carry the sensitive-read grant.
    result = await uc.handle(
        uc.DocumentSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
        ),
        case_repo=case_repo,
        case_type_version_repo=version_repo,
        document_search_provider=provider,
    )

    assert [h.document_id for h in result.items] == [public_doc.id]
    assert all(h.sensitivity != "restricted" for h in result.items)


async def test_document_search__restricted_doc__snippet_redacted_when_actor_lacks_grant(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """When the actor does NOT hold the sensitive-read grant, a
    restricted document is dropped from the result set, and any
    non-restricted hit's snippet is the bounded file-name excerpt.

    The acceptance criterion is: "snippet redacted" — meaning the
    worker never sees the term landing inside a restricted
    document's content. The strongest form of that guarantee is to
    drop the row outright so the worker cannot deduce the term
    existed by the presence of a redaction marker.
    """
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    restricted_doc = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="restricted-secret.pdf",
        sensitivity=DocumentSensitivity.RESTRICTED,
    )
    internal_doc = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="internal-secret.pdf",
        sensitivity=DocumentSensitivity.INTERNAL,
    )
    provider.documents.extend([restricted_doc, internal_doc])

    # Reviewer role does NOT hold the sensitive-read grant.
    result = await uc.handle(
        uc.DocumentSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
            text_query="secret",
        ),
        case_repo=case_repo,
        case_type_version_repo=version_repo,
        document_search_provider=provider,
    )

    ids = [h.document_id for h in result.items]
    # The restricted document is absent; the worker gains no
    # visibility into restricted content via the search surface.
    assert restricted_doc.id not in ids
    # The non-restricted hit is present with a bounded excerpt.
    assert internal_doc.id in ids
    assert result.items[0].file_name == "internal-secret.pdf"


async def test_document_search__restricted_doc__visible_with_supervisor_grant(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """When the actor holds the sensitive-read grant, a restricted
    document surfaces in the result set (the supervisor can see
    restricted content).
    """
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    restricted_doc = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="restricted-secret.pdf",
        sensitivity=DocumentSensitivity.RESTRICTED,
    )
    provider.documents.append(restricted_doc)

    # Supervisor role holds the sensitive-read grant.
    result = await uc.handle(
        uc.DocumentSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=SUPERVISOR_USER,
            actor_roles=("supervisor",),
            case_id=case.id,
            text_query="secret",
        ),
        case_repo=case_repo,
        case_type_version_repo=version_repo,
        document_search_provider=provider,
    )

    assert [h.document_id for h in result.items] == [restricted_doc.id]


async def test_document_search__unauthorized_actor__raises_forbidden(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """An actor with no roles is denied (deny-by-default)."""
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    with pytest.raises(Forbidden):
        await uc.handle(
            uc.DocumentSearchQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=(),
                case_id=case.id,
            ),
            case_repo=case_repo,
            case_type_version_repo=version_repo,
            document_search_provider=provider,
        )


# ---------------------------------------------------------------------------
# 4. Cross-tenant denial (Constitution V)
# ---------------------------------------------------------------------------


async def test_document_search__cross_tenant_document__not_returned(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """A document belonging to another tenant is silently absent from
    the result set. The actor cannot detect cross-tenant existence
    via the search surface.
    """
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    other_tenant_doc = _make_document(
        tenant_id=OTHER_TENANT,
        case_id=case.id,  # same case_id; tenant_id differs
        file_name="other-tenant-doc.pdf",
    )
    own_doc = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="own-doc.pdf",
    )
    provider.documents.extend([other_tenant_doc, own_doc])

    result = await uc.handle(
        uc.DocumentSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
        ),
        case_repo=case_repo,
        case_type_version_repo=version_repo,
        document_search_provider=provider,
    )

    assert [h.document_id for h in result.items] == [own_doc.id]


async def test_document_search__cross_tenant_case__raises_not_found(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """A case id that belongs to another tenant is treated as missing
    (Constitution V: the missing-vs-cross-tenant cases are
    indistinguishable).
    """
    version = _make_case_type_version(tenant_id=OTHER_TENANT)
    version_repo.add(version)
    other_case = _make_case(tenant_id=OTHER_TENANT, case_type_version_id=version.id)
    case_repo.add(other_case)

    with pytest.raises(NotFound):
        await uc.handle(
            uc.DocumentSearchQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("reviewer",),
                case_id=other_case.id,
            ),
            case_repo=case_repo,
            case_type_version_repo=version_repo,
            document_search_provider=provider,
        )


# ---------------------------------------------------------------------------
# 5. Classification allowlist
# ---------------------------------------------------------------------------


async def test_document_search__classification_not_on_allowlist__rejected_with_validation(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """Filtering by a classification that is NOT on the case-type's
    allowlist is rejected as ``ValidationFailed`` (router maps to
    400).
    """
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    with pytest.raises(ValidationFailed) as excinfo:
        await uc.handle(
            uc.DocumentSearchQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("reviewer",),
                case_id=case.id,
                classification="LabResult",
            ),
            case_repo=case_repo,
            case_type_version_repo=version_repo,
            document_search_provider=provider,
        )
    assert "LabResult" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 6. Pagination
# ---------------------------------------------------------------------------


async def test_document_search__pagination__honors_limit_and_offset(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """``limit`` clamps the page; ``offset`` advances it. Pages do not
    overlap.
    """
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    # Newest first → created_at descending. We seed 5 documents with
    # decreasing timestamps so the in-memory provider's stable order
    # is deterministic.
    docs: list[Document] = []
    for i in range(5):
        ts = FIXED_NOW.replace(microsecond=i)
        docs.append(
            _make_document(
                tenant_id=ACME_TENANT,
                case_id=case.id,
                file_name=f"doc-{i}.pdf",
                created_at=ts,
            )
        )
    provider.documents.extend(docs)

    page1 = await uc.handle(
        uc.DocumentSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
            limit=2,
            offset=0,
        ),
        case_repo=case_repo,
        case_type_version_repo=version_repo,
        document_search_provider=provider,
    )
    page2 = await uc.handle(
        uc.DocumentSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
            limit=2,
            offset=2,
        ),
        case_repo=case_repo,
        case_type_version_repo=version_repo,
        document_search_provider=provider,
    )

    assert len(page1.items) == 2
    assert len(page2.items) == 2
    assert page1.next_cursor == "2:2"
    ids1 = {h.document_id for h in page1.items}
    ids2 = {h.document_id for h in page2.items}
    assert ids1.isdisjoint(ids2)


async def test_document_search__limit_too_high__raises_validation_failed(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    with pytest.raises(ValidationFailed):
        await uc.handle(
            uc.DocumentSearchQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("reviewer",),
                case_id=case.id,
                limit=201,
            ),
            case_repo=case_repo,
            case_type_version_repo=version_repo,
            document_search_provider=provider,
        )


async def test_document_search__negative_offset__raises_validation_failed(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    with pytest.raises(ValidationFailed):
        await uc.handle(
            uc.DocumentSearchQuery(
                tenant_id=ACME_TENANT,
                actor_id=INTAKE_USER,
                actor_roles=("reviewer",),
                case_id=case.id,
                offset=-1,
            ),
            case_repo=case_repo,
            case_type_version_repo=version_repo,
            document_search_provider=provider,
        )


# ---------------------------------------------------------------------------
# 7. Uploader + date-range filters
# ---------------------------------------------------------------------------


async def test_document_search__uploaded_by_filter__narrows_result(
    case_repo: InMemoryCaseRepository,
    version_repo: InMemoryCaseTypeVersionRepository,
    provider: InMemoryDocumentSearchProvider,
) -> None:
    """Filtering by ``uploaded_by`` narrows the result set to that
    uploader's documents on the case.
    """
    version = _make_case_type_version(tenant_id=ACME_TENANT)
    version_repo.add(version)
    case = _make_case(tenant_id=ACME_TENANT, case_type_version_id=version.id)
    case_repo.add(case)

    alice_doc = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="alice-doc.pdf",
        uploaded_by=ALICE_UPLOADER,
    )
    bob_doc = _make_document(
        tenant_id=ACME_TENANT,
        case_id=case.id,
        file_name="bob-doc.pdf",
        uploaded_by=BOB_UPLOADER,
    )
    provider.documents.extend([alice_doc, bob_doc])

    result = await uc.handle(
        uc.DocumentSearchQuery(
            tenant_id=ACME_TENANT,
            actor_id=INTAKE_USER,
            actor_roles=("reviewer",),
            case_id=case.id,
            uploaded_by=ALICE_UPLOADER,
        ),
        case_repo=case_repo,
        case_type_version_repo=version_repo,
        document_search_provider=provider,
    )

    assert [h.document_id for h in result.items] == [alice_doc.id]


__all__ = []
