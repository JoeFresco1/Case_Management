"""Use-case tests for the viewer pipeline (T5 / FM-023 ext).

Covers ``app.application.documents.viewer``: annotation overlay CRUD
(bytes never touched), checkout/checkin with holder + expiry
conflicts, rendition burns preserving the original, the unified
revision history, and the re-index request. In-memory fakes for the
three viewer ports keep the suite hermetic (no DB).

Style: ``test_<unit>_<scenario>__<expectation>`` with
``// given`` / ``// when`` / ``// then`` sections.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.application.documents import viewer as viewer_uc
from app.domain.annotations import (
    Annotation,
    CheckoutConflict,
    CheckoutLock,
    Rendition,
)
from app.domain.document import Document, DocumentSensitivity
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from tests.application.cases._fakes import (
    FixedClock,
    InMemoryAuditStore,
    InMemoryOutbox,
)
from tests.application.documents._fakes import (
    InMemoryDocumentRepository,
    InMemoryDocumentStore,
)

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
CASE = UUID("33333333-3333-3333-3333-333333333333")
ALICE = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BOB = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
GEOMETRY: dict[str, object] = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05}
SHA_V1 = "a" * 64

REVIEWER = ("reviewer",)
PROCESS_ADMIN = ("process_admin",)
INTAKE = ("intake",)


class InMemoryAnnotationRepository:
    """In-memory ``AnnotationRepository`` keyed by annotation id."""

    def __init__(self) -> None:
        self._rows: dict[UUID, Annotation] = {}

    async def add(self, annotation: Annotation) -> None:
        self._rows[annotation.id] = annotation

    async def get(self, tenant_id: UUID, annotation_id: UUID) -> Annotation | None:
        row = self._rows.get(annotation_id)
        return row if row is not None and row.tenant_id == tenant_id else None

    async def list_for_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        document_version: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Annotation]:
        rows = [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id
            and r.document_id == document_id
            and (document_version is None or r.document_version == document_version)
        ]
        rows.sort(key=lambda r: r.created_at)
        return rows[offset : offset + limit]

    async def save(self, annotation: Annotation) -> None:
        self._rows[annotation.id] = annotation

    async def delete(self, tenant_id: UUID, annotation_id: UUID) -> bool:
        row = self._rows.get(annotation_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        del self._rows[annotation_id]
        return True


class InMemoryCheckoutRepository:
    """In-memory ``CheckoutLockRepository`` preserving full history."""

    def __init__(self) -> None:
        self._rows: list[CheckoutLock] = []

    async def add(self, lock: CheckoutLock) -> None:
        self._rows.append(lock)

    async def get_active(self, tenant_id: UUID, document_id: UUID) -> CheckoutLock | None:
        candidates = [
            r
            for r in self._rows
            if r.tenant_id == tenant_id and r.document_id == document_id and r.released_at is None
        ]
        candidates.sort(key=lambda r: r.acquired_at, reverse=True)
        return candidates[0] if candidates else None

    async def save(self, lock: CheckoutLock) -> None:
        for idx, row in enumerate(self._rows):
            if row.id == lock.id:
                self._rows[idx] = lock
                return
        self._rows.append(lock)

    async def list_history(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[CheckoutLock]:
        rows = [r for r in self._rows if r.tenant_id == tenant_id and r.document_id == document_id]
        rows.sort(key=lambda r: r.acquired_at, reverse=True)
        return rows[offset : offset + limit]


class InMemoryRenditionRepository:
    """In-memory ``RenditionRepository``."""

    def __init__(self) -> None:
        self._rows: list[Rendition] = []

    async def add(self, rendition: Rendition) -> None:
        self._rows.append(rendition)

    async def list_for_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Rendition]:
        rows = [r for r in self._rows if r.tenant_id == tenant_id and r.document_id == document_id]
        rows.sort(key=lambda r: r.created_at)
        return rows[offset : offset + limit]


def _document(*, tenant_id: UUID = TENANT) -> Document:
    """Build one v1 document aggregate for the fixed case."""
    return Document.create(
        tenant_id=tenant_id,
        case_id=CASE,
        file_name="brief.pdf",
        content_type="application/pdf",
        classification="Evidence",
        sensitivity=DocumentSensitivity.INTERNAL,
        sha256=SHA_V1,
        size=1024,
        storage_ref="store/v1",
        uploaded_by=ALICE,
        now=NOW,
    )


def _seed_document(doc_repo: InMemoryDocumentRepository, doc: Document) -> None:
    """Stage a v1 document + version pair directly in the fake."""
    from app.domain.document import DocumentVersion  # noqa: PLC0415

    doc_repo._docs[(doc.tenant_id, doc.id)] = doc
    doc_repo._versions.setdefault((doc.tenant_id, doc.id), []).append(
        DocumentVersion(
            id=uuid4(),
            tenant_id=doc.tenant_id,
            document_id=doc.id,
            version_number=1,
            sha256=doc.sha256,
            size=doc.size,
            storage_ref=doc.storage_ref,
            content_type=doc.content_type,
            uploaded_by=doc.uploaded_by,
            uploaded_at=doc.created_at,
        )
    )


# ---------------------------------------------------------------------------
# Annotation CRUD — overlay only, original bytes never touched
# ---------------------------------------------------------------------------


async def test_create_annotation__valid__staged_without_touching_document() -> None:
    """Creating markup stages an overlay row; the document row is unchanged."""
    # given:
    doc_repo, ann_repo, audit, outbox = (
        InMemoryDocumentRepository(),
        InMemoryAnnotationRepository(),
        InMemoryAuditStore(),
        InMemoryOutbox(),
    )
    doc = _document()
    _seed_document(doc_repo, doc)

    # when:
    annotation = await viewer_uc.create_annotation(
        viewer_uc.CreateAnnotationCommand(
            tenant_id=TENANT,
            actor_id=ALICE,
            actor_roles=REVIEWER,
            document_id=doc.id,
            document_version=1,
            page=2,
            kind="highlight",
            geometry=dict(GEOMETRY),
            content=None,
        ),
        document_repo=doc_repo,
        annotation_repo=ann_repo,
        audit=audit,
        outbox=outbox,
        clock=FixedClock(NOW),
    )

    # then:
    assert annotation.document_version == 1
    stored = await doc_repo.get(TENANT, doc.id)
    assert stored is not None
    assert stored.sha256 == SHA_V1
    assert stored.current_version == 1
    assert stored.storage_ref == "store/v1"
    assert [e.event_type for e in audit.events] == [viewer_uc.ANNOTATION_CREATED]
    assert [e.event_type for e in outbox.events] == [viewer_uc.ANNOTATION_CREATED]


async def test_create_annotation__unknown_version__rejects() -> None:
    """A mark pinned to a version that does not exist is refused."""
    # given:
    doc_repo, ann_repo = InMemoryDocumentRepository(), InMemoryAnnotationRepository()
    doc = _document()
    _seed_document(doc_repo, doc)

    # when/then:
    with pytest.raises(ValidationFailed, match="document_version 9"):
        await viewer_uc.create_annotation(
            viewer_uc.CreateAnnotationCommand(
                tenant_id=TENANT,
                actor_id=ALICE,
                actor_roles=REVIEWER,
                document_id=doc.id,
                document_version=9,
                page=1,
                kind="stamp",
                geometry=dict(GEOMETRY),
                content="APPROVED",
            ),
            document_repo=doc_repo,
            annotation_repo=ann_repo,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(NOW),
        )


async def test_create_annotation__cross_tenant_document__not_found() -> None:
    """A document id from another tenant never confirms existence."""
    # given:
    doc_repo, ann_repo = InMemoryDocumentRepository(), InMemoryAnnotationRepository()
    doc = _document()
    _seed_document(doc_repo, doc)

    # when/then:
    with pytest.raises(NotFound):
        await viewer_uc.create_annotation(
            viewer_uc.CreateAnnotationCommand(
                tenant_id=OTHER_TENANT,
                actor_id=ALICE,
                actor_roles=REVIEWER,
                document_id=doc.id,
                document_version=1,
                page=1,
                kind="highlight",
                geometry=dict(GEOMETRY),
                content=None,
            ),
            document_repo=doc_repo,
            annotation_repo=ann_repo,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(NOW),
        )


async def test_create_annotation__intake_role__forbidden() -> None:
    """Actors without ``document.annotate`` cannot create markup."""
    # given:
    doc_repo, ann_repo = InMemoryDocumentRepository(), InMemoryAnnotationRepository()
    doc = _document()
    _seed_document(doc_repo, doc)

    # when/then:
    with pytest.raises(Forbidden):
        await viewer_uc.create_annotation(
            viewer_uc.CreateAnnotationCommand(
                tenant_id=TENANT,
                actor_id=ALICE,
                actor_roles=INTAKE,
                document_id=doc.id,
                document_version=1,
                page=1,
                kind="highlight",
                geometry=dict(GEOMETRY),
                content=None,
            ),
            document_repo=doc_repo,
            annotation_repo=ann_repo,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(NOW),
        )


async def test_update_annotation__geometry__revalidated_and_audited() -> None:
    """Overlay edits re-validate kind/content pairing and audit the change."""
    # given:
    doc_repo, ann_repo, audit = (
        InMemoryDocumentRepository(),
        InMemoryAnnotationRepository(),
        InMemoryAuditStore(),
    )
    doc = _document()
    _seed_document(doc_repo, doc)
    created = await viewer_uc.create_annotation(
        viewer_uc.CreateAnnotationCommand(
            tenant_id=TENANT,
            actor_id=ALICE,
            actor_roles=REVIEWER,
            document_id=doc.id,
            document_version=1,
            page=1,
            kind="text_box",
            geometry=dict(GEOMETRY),
            content="note",
        ),
        document_repo=doc_repo,
        annotation_repo=ann_repo,
        audit=audit,
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )

    # when:
    updated = await viewer_uc.update_annotation(
        viewer_uc.UpdateAnnotationCommand(
            tenant_id=TENANT,
            actor_id=ALICE,
            actor_roles=REVIEWER,
            document_id=doc.id,
            annotation_id=created.id,
            geometry={"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.1},
            content="edited",
        ),
        document_repo=doc_repo,
        annotation_repo=ann_repo,
        audit=audit,
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )

    # then:
    assert updated.geometry["x"] == 0.5
    assert updated.content == "edited"
    assert updated.created_by == ALICE
    assert audit.events[-1].event_type == viewer_uc.ANNOTATION_UPDATED


async def test_delete_annotation__removed__document_untouched() -> None:
    """Removal deletes the overlay row only; the document row is unchanged."""
    # given:
    doc_repo, ann_repo, audit = (
        InMemoryDocumentRepository(),
        InMemoryAnnotationRepository(),
        InMemoryAuditStore(),
    )
    doc = _document()
    _seed_document(doc_repo, doc)
    created = await viewer_uc.create_annotation(
        viewer_uc.CreateAnnotationCommand(
            tenant_id=TENANT,
            actor_id=ALICE,
            actor_roles=REVIEWER,
            document_id=doc.id,
            document_version=1,
            page=1,
            kind="highlight",
            geometry=dict(GEOMETRY),
            content=None,
        ),
        document_repo=doc_repo,
        annotation_repo=ann_repo,
        audit=audit,
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )

    # when:
    removed_id = await viewer_uc.delete_annotation(
        viewer_uc.DeleteAnnotationCommand(
            tenant_id=TENANT,
            actor_id=ALICE,
            actor_roles=REVIEWER,
            document_id=doc.id,
            annotation_id=created.id,
        ),
        document_repo=doc_repo,
        annotation_repo=ann_repo,
        audit=audit,
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )

    # then:
    assert removed_id == created.id
    assert await ann_repo.get(TENANT, created.id) is None
    stored = await doc_repo.get(TENANT, doc.id)
    assert stored is not None
    assert stored.sha256 == SHA_V1
    assert audit.events[-1].event_type == viewer_uc.ANNOTATION_DELETED


# ---------------------------------------------------------------------------
# Checkout / checkin — holder + expiry conflict contract
# ---------------------------------------------------------------------------


async def test_checkout__fresh__live_lock_with_holder() -> None:
    """A fresh checkout records holder + expiry and audits the event."""
    # given:
    doc_repo, lock_repo, audit = (
        InMemoryDocumentRepository(),
        InMemoryCheckoutRepository(),
        InMemoryAuditStore(),
    )
    doc = _document()
    _seed_document(doc_repo, doc)

    # when:
    result = await viewer_uc.checkout_document(
        viewer_uc.CheckoutCommand(
            tenant_id=TENANT, actor_id=ALICE, actor_roles=REVIEWER, document_id=doc.id
        ),
        document_repo=doc_repo,
        checkout_repo=lock_repo,
        audit=audit,
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )

    # then:
    assert result.renewed is False
    assert result.lock.holder_user_id == ALICE
    assert result.lock.is_live(now=NOW) is True
    assert audit.events[-1].event_type == viewer_uc.DOCUMENT_CHECKED_OUT


async def test_checkout__foreign_live_lock__conflict_carries_holder_and_expiry() -> None:
    """The T5 acceptance: a foreign live lock conflicts with holder + expiry."""
    # given:
    doc_repo, lock_repo = InMemoryDocumentRepository(), InMemoryCheckoutRepository()
    doc = _document()
    _seed_document(doc_repo, doc)
    first = await viewer_uc.checkout_document(
        viewer_uc.CheckoutCommand(
            tenant_id=TENANT, actor_id=ALICE, actor_roles=REVIEWER, document_id=doc.id
        ),
        document_repo=doc_repo,
        checkout_repo=lock_repo,
        audit=InMemoryAuditStore(),
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )

    # when/then:
    with pytest.raises(CheckoutConflict) as excinfo:
        await viewer_uc.checkout_document(
            viewer_uc.CheckoutCommand(
                tenant_id=TENANT, actor_id=BOB, actor_roles=REVIEWER, document_id=doc.id
            ),
            document_repo=doc_repo,
            checkout_repo=lock_repo,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(NOW + timedelta(minutes=1)),
        )
    assert excinfo.value.holder == ALICE
    assert excinfo.value.expires_at == first.lock.expires_at
    assert excinfo.value.to_details()["holder"] == str(ALICE)


async def test_checkout__same_holder__renews_in_place() -> None:
    """Re-checkout by the holder renews the same row (no duplicate)."""
    # given:
    doc_repo, lock_repo = InMemoryDocumentRepository(), InMemoryCheckoutRepository()
    doc = _document()
    _seed_document(doc_repo, doc)
    first = await viewer_uc.checkout_document(
        viewer_uc.CheckoutCommand(
            tenant_id=TENANT, actor_id=ALICE, actor_roles=REVIEWER, document_id=doc.id
        ),
        document_repo=doc_repo,
        checkout_repo=lock_repo,
        audit=InMemoryAuditStore(),
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )

    # when:
    second = await viewer_uc.checkout_document(
        viewer_uc.CheckoutCommand(
            tenant_id=TENANT,
            actor_id=ALICE,
            actor_roles=REVIEWER,
            document_id=doc.id,
            lease_seconds=7200,
        ),
        document_repo=doc_repo,
        checkout_repo=lock_repo,
        audit=InMemoryAuditStore(),
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW + timedelta(minutes=5)),
    )

    # then:
    assert second.renewed is True
    assert second.lock.id == first.lock.id
    assert second.lock.expires_at == NOW + timedelta(minutes=5) + timedelta(seconds=7200)


async def test_checkin__holder__released_and_visible_in_history() -> None:
    """Checkin stamps the release; the row stays for checkout history."""
    # given:
    doc_repo, lock_repo, audit = (
        InMemoryDocumentRepository(),
        InMemoryCheckoutRepository(),
        InMemoryAuditStore(),
    )
    doc = _document()
    _seed_document(doc_repo, doc)
    await viewer_uc.checkout_document(
        viewer_uc.CheckoutCommand(
            tenant_id=TENANT, actor_id=ALICE, actor_roles=REVIEWER, document_id=doc.id
        ),
        document_repo=doc_repo,
        checkout_repo=lock_repo,
        audit=audit,
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )

    # when:
    result = await viewer_uc.checkin_document(
        viewer_uc.CheckinCommand(
            tenant_id=TENANT, actor_id=ALICE, actor_roles=REVIEWER, document_id=doc.id
        ),
        document_repo=doc_repo,
        checkout_repo=lock_repo,
        audit=audit,
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW + timedelta(minutes=5)),
    )

    # then:
    assert result.lock.released_at == NOW + timedelta(minutes=5)
    history = await lock_repo.list_history(TENANT, doc.id)
    assert len(history) == 1
    assert audit.events[-1].event_type == viewer_uc.DOCUMENT_CHECKED_IN


async def test_checkin__foreign_holder__conflict_names_holder() -> None:
    """Only the holder may release; others learn holder + expiry."""
    # given:
    doc_repo, lock_repo = InMemoryDocumentRepository(), InMemoryCheckoutRepository()
    doc = _document()
    _seed_document(doc_repo, doc)
    await viewer_uc.checkout_document(
        viewer_uc.CheckoutCommand(
            tenant_id=TENANT, actor_id=ALICE, actor_roles=REVIEWER, document_id=doc.id
        ),
        document_repo=doc_repo,
        checkout_repo=lock_repo,
        audit=InMemoryAuditStore(),
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )

    # when/then:
    with pytest.raises(CheckoutConflict) as excinfo:
        await viewer_uc.checkin_document(
            viewer_uc.CheckinCommand(
                tenant_id=TENANT, actor_id=BOB, actor_roles=REVIEWER, document_id=doc.id
            ),
            document_repo=doc_repo,
            checkout_repo=lock_repo,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(NOW + timedelta(minutes=1)),
        )
    assert excinfo.value.holder == ALICE


# ---------------------------------------------------------------------------
# Renditions + revision history + re-index
# ---------------------------------------------------------------------------


async def test_burn_rendition__new_bytes__original_preserved() -> None:
    """A burn stores new bytes + a rendition row; version rows are untouched."""
    # given:
    doc_repo, rend_repo, store, audit = (
        InMemoryDocumentRepository(),
        InMemoryRenditionRepository(),
        InMemoryDocumentStore(),
        InMemoryAuditStore(),
    )
    doc = _document()
    _seed_document(doc_repo, doc)
    redacted = b"%PDF redacted bytes"

    # when:
    rendition = await viewer_uc.burn_rendition(
        viewer_uc.BurnRenditionCommand(
            tenant_id=TENANT,
            actor_id=ALICE,
            actor_roles=PROCESS_ADMIN,
            document_id=doc.id,
            source_version=1,
            content=redacted,
            content_type="application/pdf",
            reason="contains account number",
            region="page 1, header",
        ),
        document_repo=doc_repo,
        rendition_repo=rend_repo,
        document_store=store,
        audit=audit,
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )

    # then:
    assert rendition.source_version == 1
    assert rendition.redaction_id is not None
    assert len(store.puts) == 1
    assert store.puts[0].content == redacted
    stored = await doc_repo.get(TENANT, doc.id)
    assert stored is not None
    assert stored.sha256 == SHA_V1
    assert stored.current_version == 1
    assert stored.storage_ref == "store/v1"
    assert audit.events[-1].event_type == viewer_uc.RENDITION_BURNED


async def test_revision_history__merges_versions_and_renditions() -> None:
    """The history view unions filed versions with burned renditions."""
    # given:
    doc_repo, rend_repo = InMemoryDocumentRepository(), InMemoryRenditionRepository()
    doc = _document()
    _seed_document(doc_repo, doc)
    await viewer_uc.burn_rendition(
        viewer_uc.BurnRenditionCommand(
            tenant_id=TENANT,
            actor_id=ALICE,
            actor_roles=PROCESS_ADMIN,
            document_id=doc.id,
            source_version=1,
            content=b"redacted",
            content_type="application/pdf",
            reason="contains account number",
        ),
        document_repo=doc_repo,
        rendition_repo=rend_repo,
        document_store=InMemoryDocumentStore(),
        audit=InMemoryAuditStore(),
        outbox=InMemoryOutbox(),
        clock=FixedClock(NOW),
    )

    # when:
    history = await viewer_uc.revision_history(
        tenant_id=TENANT,
        actor_id=ALICE,
        actor_roles=REVIEWER,
        document_id=doc.id,
        document_repo=doc_repo,
        rendition_repo=rend_repo,
    )

    # then:
    kinds = [(e.entry_type, e.version_number) for e in history.entries()]
    assert ("version", 1) in kinds
    assert ("rendition", 1) in kinds


async def test_request_reindex__valid__records_request_and_audit() -> None:
    """A reasoned re-index request validates and audits the rebuild hook."""
    # given:
    doc_repo, audit, outbox = (
        InMemoryDocumentRepository(),
        InMemoryAuditStore(),
        InMemoryOutbox(),
    )
    doc = _document()
    _seed_document(doc_repo, doc)

    # when:
    request = await viewer_uc.request_reindex(
        viewer_uc.ReindexCommand(
            tenant_id=TENANT,
            actor_id=ALICE,
            actor_roles=REVIEWER,
            document_id=doc.id,
            reason="post-edit catalog refresh",
        ),
        document_repo=doc_repo,
        audit=audit,
        outbox=outbox,
        clock=FixedClock(NOW),
    )

    # then:
    assert request.reason == "post-edit catalog refresh"
    assert audit.events[-1].event_type == viewer_uc.DOCUMENT_REINDEXED
    assert outbox.events[-1].event_type == viewer_uc.DOCUMENT_REINDEXED


async def test_request_reindex__blank_reason__rejects() -> None:
    """The rebuild audit needs a rationale, so blank reasons are refused."""
    # given:
    doc_repo = InMemoryDocumentRepository()
    doc = _document()
    _seed_document(doc_repo, doc)

    # when/then:
    with pytest.raises(ValidationFailed, match="reason is required"):
        await viewer_uc.request_reindex(
            viewer_uc.ReindexCommand(
                tenant_id=TENANT,
                actor_id=ALICE,
                actor_roles=REVIEWER,
                document_id=doc.id,
                reason="   ",
            ),
            document_repo=doc_repo,
            audit=InMemoryAuditStore(),
            outbox=InMemoryOutbox(),
            clock=FixedClock(NOW),
        )
