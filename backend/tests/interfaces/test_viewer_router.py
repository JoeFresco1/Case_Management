"""SQLite endpoint coverage for the viewer HTTP boundary (T5 / FM-023 ext).

Exercises the production provider composition
(``SqlViewerHttpDependencyProvider``) end-to-end: annotation overlay
CRUD (original bytes never mutated), checkout/checkin with structured
409 conflicts (holder + expiry), rendition burns + unified revision
history, the re-index request, tenant isolation, authorization denial,
and OpenAPI surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import UserContext, get_current_user, get_db
from app.domain.errors import DomainError
from app.infrastructure.persistence.viewer_http_dependencies import (
    SqlViewerHttpDependencyProvider,
)
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.viewer import router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
ALICE = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BOB = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
SHA_V1 = "a" * 64


def _run(coro: Any) -> Any:
    """Run an async seed operation for the synchronous TestClient test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeDocumentStore:
    """In-memory ``DocumentStore`` for burn tests (bytes stay in a dict)."""

    def __init__(self) -> None:
        self.puts: list[bytes] = []
        self._by_ref: dict[str, bytes] = {}

    async def put(self, *, tenant_id: UUID, content: bytes, content_type: str, sha256: str) -> str:
        ref = f"{tenant_id}/{sha256[:2]}/{sha256}"
        self.puts.append(content)
        self._by_ref[ref] = content
        return ref

    async def get(self, *, tenant_id: UUID, content_ref: str) -> bytes:
        return self._by_ref[content_ref]


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    store: _FakeDocumentStore,
    *,
    roles: tuple[str, ...] = ("process_admin",),
    user_id: UUID = ALICE,
    tenant_id: UUID = TENANT_A,
) -> FastAPI:
    """Build a minimal app using the production viewer provider."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.viewer_http_dependencies = SqlViewerHttpDependencyProvider()
    app.state.document_store = store
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic User",
        roles=roles,
    )
    return app


async def _seed_document(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID = TENANT_A,
) -> UUID:
    """Seed tenant → case type → version → case → v1 document; return doc id."""
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        Case as CaseRow,
    )
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        CaseType as CaseTypeRow,
    )
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        CaseTypeVersion as CaseTypeVersionRow,
    )
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        Document as DocumentRow,
    )
    from app.infrastructure.persistence.models import (  # noqa: PLC0415
        DocumentVersion as DocumentVersionRow,
    )
    from app.infrastructure.persistence.models import Tenant as TenantRow  # noqa: PLC0415

    case_type_id, version_id, case_id, doc_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with session_factory() as session:
        session.add(
            TenantRow(
                id=tenant_id,
                key=f"T{tenant_id.hex[:6]}",
                name=f"Tenant {tenant_id}",
                created_at=NOW,
            )
        )
        session.add(
            CaseTypeRow(
                id=case_type_id,
                tenant_id=tenant_id,
                key="ServiceRequest",
                name="ServiceRequest",
                description="",
                sensitivity_default="internal",
                current_version_id=version_id,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            CaseTypeVersionRow(
                id=version_id,
                tenant_id=tenant_id,
                case_type_id=case_type_id,
                version_number=1,
                schema_json={"fields": []},
                document_classifications=["Evidence"],
                rules=[],
                status="published",
                published_at=NOW,
                published_by=ALICE,
                created_at=NOW,
            )
        )
        session.add(
            CaseRow(
                id=case_id,
                tenant_id=tenant_id,
                reference="C-TEST-2026-000001",
                case_type_version_id=version_id,
                status="open",
                priority="normal",
                title="Viewer case",
                description="",
                data={},
                current_workflow_state="intake",
                version_token=1,
                created_by=ALICE,
                created_at=NOW,
                updated_at=NOW,
                closed_at=None,
            )
        )
        session.add(
            DocumentRow(
                id=doc_id,
                tenant_id=tenant_id,
                case_id=case_id,
                file_name="brief.pdf",
                content_type="application/pdf",
                classification="Evidence",
                sensitivity="internal",
                sha256=SHA_V1,
                size=1024,
                current_version=1,
                storage_ref="store/v1",
                uploaded_by=ALICE,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            DocumentVersionRow(
                id=uuid4(),
                tenant_id=tenant_id,
                document_id=doc_id,
                version_number=1,
                sha256=SHA_V1,
                size=1024,
                storage_ref="store/v1",
                content_type="application/pdf",
                uploaded_by=ALICE,
                uploaded_at=NOW,
            )
        )
        await session.commit()
    return doc_id


def _doc_row(session_factory: async_sessionmaker[AsyncSession], doc_id: UUID) -> Any:
    """Fetch the raw document ORM row to assert bytes were never mutated."""

    async def _get() -> Any:
        from sqlalchemy import select  # noqa: PLC0415

        from app.infrastructure.persistence.models import (  # noqa: PLC0415
            Document as DocumentRow,
        )

        async with session_factory() as session:
            result = await session.execute(select(DocumentRow).where(DocumentRow.id == doc_id))
            return result.scalar_one()

    return _run(_get())


def test_viewer_router__annotation_crud__overlay_only_original_bytes_preserved(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Annotation create/update/delete round-trips; original bytes never change."""
    # given:
    store = _FakeDocumentStore()
    app = _build_app(sql_session_factory, store)
    doc_id = _run(_seed_document(sql_session_factory))
    headers = {"Idempotency-Key": str(uuid4())}

    with TestClient(app) as client:
        # when: create
        created = client.post(
            f"/api/v1/documents/{doc_id}/annotations",
            json={
                "document_version": 1,
                "page": 2,
                "kind": "highlight",
                "geometry": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05},
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        annotation_id = created.json()["id"]

        # when: list + update + delete
        listed = client.get(f"/api/v1/documents/{doc_id}/annotations")
        assert listed.status_code == 200, listed.text
        assert [a["id"] for a in listed.json()] == [annotation_id]

        updated = client.patch(
            f"/api/v1/documents/{doc_id}/annotations/{annotation_id}",
            json={"content": "caption"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["content"] == "caption"

        deleted = client.delete(
            f"/api/v1/documents/{doc_id}/annotations/{annotation_id}",
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert deleted.status_code == 204, deleted.text

    # then: the document row still points at the original bytes
    row = _doc_row(sql_session_factory, doc_id)
    assert row.sha256 == SHA_V1
    assert row.current_version == 1
    assert row.storage_ref == "store/v1"
    assert store.puts == []


def test_viewer_router__checkout_conflict__structured_holder_and_expiry(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A foreign checkout loses with 409 carrying holder + expires_at."""
    # given:
    store = _FakeDocumentStore()
    doc_id = _run(_seed_document(sql_session_factory))
    alice_app = _build_app(sql_session_factory, store, user_id=ALICE)
    bob_app = _build_app(sql_session_factory, store, user_id=BOB)

    with TestClient(alice_app) as alice:
        first = alice.post(
            f"/api/v1/documents/{doc_id}/checkout",
            json={"lease_seconds": 3600},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert first.status_code == 201, first.text

    # when: Bob checks out under Alice's live lock
    with TestClient(bob_app) as bob:
        conflict = bob.post(
            f"/api/v1/documents/{doc_id}/checkout",
            json={"lease_seconds": 3600},
            headers={"Idempotency-Key": str(uuid4())},
        )

    # then: structured conflict with holder + expiry
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    assert detail["code"] == "conflict"
    assert detail["reason"] == "checkout_conflict"
    assert detail["holder"] == str(ALICE)
    assert "expires_at" in detail
    assert detail["document_id"] == str(doc_id)


def test_viewer_router__checkout_checkin_history__holder_round_trip(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Checkout → renew → checkin → history shows the full trail."""
    # given:
    store = _FakeDocumentStore()
    app = _build_app(sql_session_factory, store)
    doc_id = _run(_seed_document(sql_session_factory))

    with TestClient(app) as client:
        first = client.post(
            f"/api/v1/documents/{doc_id}/checkout",
            json={"lease_seconds": 3600},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert first.status_code == 201, first.text
        assert first.json()["renewed"] is False

        renewed = client.post(
            f"/api/v1/documents/{doc_id}/checkout",
            json={"lease_seconds": 7200},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert renewed.status_code == 201, renewed.text
        assert renewed.json()["renewed"] is True

        checkin = client.post(
            f"/api/v1/documents/{doc_id}/checkin",
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert checkin.status_code == 200, checkin.text
        assert checkin.json()["released_at"] is not None

        history = client.get(f"/api/v1/documents/{doc_id}/checkout/history")
        assert history.status_code == 200, history.text
        assert len(history.json()) == 1
        assert history.json()[0]["holder_user_id"] == str(ALICE)


def test_viewer_router__burn_and_revisions__rendition_listed_original_kept(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A rendition burn stores new bytes; revisions unions versions + burns."""
    # given:
    store = _FakeDocumentStore()
    app = _build_app(sql_session_factory, store)
    doc_id = _run(_seed_document(sql_session_factory))

    with TestClient(app) as client:
        burned = client.post(
            f"/api/v1/documents/{doc_id}/renditions",
            files={"content": ("redacted.pdf", b"%PDF redacted", "application/pdf")},
            data={"source_version": "1", "reason": "contains account number"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert burned.status_code == 201, burned.text
        assert burned.json()["kind"] == "redacted"
        assert burned.json()["source_version"] == 1

        renditions = client.get(f"/api/v1/documents/{doc_id}/renditions")
        assert renditions.status_code == 200, renditions.text
        assert len(renditions.json()) == 1

        revisions = client.get(f"/api/v1/documents/{doc_id}/revisions")
        assert revisions.status_code == 200, revisions.text
        kinds = [(e["entry_type"], e["version_number"]) for e in revisions.json()["entries"]]
        assert ("version", 1) in kinds
        assert ("rendition", 1) in kinds

    # then: original bytes preserved, burned bytes stored separately
    row = _doc_row(sql_session_factory, doc_id)
    assert row.sha256 == SHA_V1
    assert row.current_version == 1
    assert store.puts == [b"%PDF redacted"]


def test_viewer_router__reindex__accepted_and_validated(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A reasoned re-index request is accepted (202); blank reason is 422."""
    # given:
    store = _FakeDocumentStore()
    app = _build_app(sql_session_factory, store)
    doc_id = _run(_seed_document(sql_session_factory))

    with TestClient(app) as client:
        accepted = client.post(
            f"/api/v1/documents/{doc_id}/reindex",
            json={"reason": "post-edit catalog refresh"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["reason"] == "post-edit catalog refresh"

        blank = client.post(
            f"/api/v1/documents/{doc_id}/reindex",
            json={"reason": ""},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert blank.status_code == 422, blank.text


def test_viewer_router__cross_tenant_document__not_found(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tenant B cannot see tenant A's document overlays (404, no leak)."""
    # given:
    store = _FakeDocumentStore()
    doc_id = _run(_seed_document(sql_session_factory, tenant_id=TENANT_A))
    other_app = _build_app(sql_session_factory, store, tenant_id=TENANT_B)

    with TestClient(other_app) as client:
        listed = client.get(f"/api/v1/documents/{doc_id}/annotations")
        assert listed.status_code == 404, listed.text

        conflict = client.post(
            f"/api/v1/documents/{doc_id}/checkout",
            json={"lease_seconds": 60},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert conflict.status_code == 404, conflict.text


def test_viewer_router__intake_role__forbidden_on_mutations(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Actors without viewer grants get 403 on overlay/checkout writes."""
    # given:
    store = _FakeDocumentStore()
    app = _build_app(sql_session_factory, store, roles=("intake",))
    doc_id = _run(_seed_document(sql_session_factory))

    with TestClient(app) as client:
        denied = client.post(
            f"/api/v1/documents/{doc_id}/annotations",
            json={
                "document_version": 1,
                "page": 1,
                "kind": "highlight",
                "geometry": {"x": 0.1},
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert denied.status_code == 403, denied.text

        checkout_denied = client.post(
            f"/api/v1/documents/{doc_id}/checkout",
            json={"lease_seconds": 60},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert checkout_denied.status_code == 403, checkout_denied.text


def test_viewer_router__missing_idempotency_key__bad_request(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutations without Idempotency-Key are refused before any DB touch."""
    # given:
    store = _FakeDocumentStore()
    app = _build_app(sql_session_factory, store)
    doc_id = _run(_seed_document(sql_session_factory))

    with TestClient(app) as client:
        refused = client.post(
            f"/api/v1/documents/{doc_id}/checkout",
            json={"lease_seconds": 60},
        )
        assert refused.status_code == 400, refused.text


def test_viewer_router__openapi__viewer_paths_present(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every viewer endpoint is registered on the OpenAPI contract."""
    # given:
    store = _FakeDocumentStore()
    app = _build_app(sql_session_factory, store)

    with TestClient(app) as client:
        spec = client.get("/openapi.json")
        assert spec.status_code == 200, spec.text
        paths = set(spec.json()["paths"])
        doc_id = "00000000-0000-0000-0000-000000000000"
        for template in (
            f"/api/v1/documents/{doc_id}/annotations",
            f"/api/v1/documents/{doc_id}/revisions",
            f"/api/v1/documents/{doc_id}/renditions",
            f"/api/v1/documents/{doc_id}/checkout",
            f"/api/v1/documents/{doc_id}/checkin",
            f"/api/v1/documents/{doc_id}/checkout/history",
            f"/api/v1/documents/{doc_id}/reindex",
        ):
            # then: the templated path (with {document_id}) is registered
            assert any(
                p.startswith("/api/v1/documents/") and p.endswith(template.split(doc_id)[1])
                for p in paths
            ), f"missing OpenAPI path for {template}"
