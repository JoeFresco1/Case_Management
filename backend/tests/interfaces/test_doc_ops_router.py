"""SQLite endpoint coverage for the T5 document-operations boundary.

Exercises the production provider composition
(``SqlCaseHttpDependencyProvider``) end-to-end: x-ref create/list
with configurable sort, separation split/join-merge plans, package
generation with manifest, duplicate-by-hash detection, and
authorized CSV export (tenant isolation + auth denial).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.documents.doc_ops import InMemoryXrefStore
from app.deps import UserContext, get_current_user, get_db
from app.domain.document import Document, DocumentSensitivity, DocumentVersion
from app.domain.errors import DomainError
from app.infrastructure.persistence.case_http_dependencies import (
    SqlCaseHttpDependencyProvider,
)
from app.infrastructure.persistence.documents import SqlDocumentRepository
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.doc_ops import get_xref_store, router

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
OWNER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CASE_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

IDEMPOTENCY = {"Idempotency-Key": "01234567-89ab-cdef-0123-456789abcdef"}


def _run(coro: Any) -> Any:
    """Run an async seed operation for the synchronous TestClient test."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sha(seed: str) -> str:
    """Return a deterministic 64-hex digest for ``seed``."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


async def _seed_document(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID = TENANT_A,
    case_id: UUID = CASE_ID,
    file_name: str = "exhibit.pdf",
    seed: str = "bytes-a",
    sensitivity: DocumentSensitivity = DocumentSensitivity.INTERNAL,
) -> UUID:
    """Stage one document + version row; return its id."""
    document = Document.create(
        tenant_id=tenant_id,
        case_id=case_id,
        file_name=file_name,
        content_type="application/pdf",
        classification="Evidence",
        sensitivity=sensitivity,
        sha256=_sha(seed),
        size=len(seed),
        storage_ref=f"store/{seed}",
        uploaded_by=OWNER_ID,
        now=NOW,
    )
    version = DocumentVersion(
        id=uuid4(),
        tenant_id=tenant_id,
        document_id=document.id,
        version_number=1,
        sha256=document.sha256,
        size=document.size,
        storage_ref=document.storage_ref,
        content_type=document.content_type,
        uploaded_by=OWNER_ID,
        uploaded_at=NOW,
    )
    async with session_factory() as session:
        await SqlDocumentRepository(session).add_document_and_version(
            document=document, version=version
        )
        await session.commit()
    return document.id


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    roles: tuple[str, ...] = ("reviewer",),
    user_id: UUID = OWNER_ID,
    tenant_id: UUID = TENANT_A,
    xref_store: InMemoryXrefStore | None = None,
) -> FastAPI:
    """Build a minimal app using the production provider composition."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.case_http_dependencies = SqlCaseHttpDependencyProvider()
    app.state.xref_store = xref_store or InMemoryXrefStore()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic User",
        roles=roles,
    )
    app.dependency_overrides[get_xref_store] = lambda: app.state.xref_store
    return app


def test_doc_ops_router__xref_create_and_list__round_trips_sorted(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Create two x-refs; list returns them in label order (T5 x-refs)."""
    # // given two documents
    source_id = _run(_seed_document(sql_session_factory, seed="xref-src"))
    target_b = _run(_seed_document(sql_session_factory, seed="xref-b", file_name="b.pdf"))
    target_a = _run(_seed_document(sql_session_factory, seed="xref-a", file_name="a.pdf"))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when creating two x-refs with distinct labels
        first = client.post(
            f"/api/v1/documents/{source_id}/xrefs",
            json={"target_document_id": str(target_b), "label": "zebra", "sort_order": 0},
            headers=IDEMPOTENCY,
        )
        assert first.status_code == 201, first.text
        second = client.post(
            f"/api/v1/documents/{source_id}/xrefs",
            json={"target_document_id": str(target_a), "label": "apple", "sort_order": 5},
            headers={**IDEMPOTENCY, "Idempotency-Key": "11111111-1111-1111-1111-111111111111"},
        )
        assert second.status_code == 201, second.text
        # // then listing by label returns deterministic display order.
        listed = client.get(f"/api/v1/documents/{source_id}/xrefs?sort_by=label")
        assert listed.status_code == 200, listed.text
        assert [x["label"] for x in listed.json()] == ["apple", "zebra"]


def test_doc_ops_router__xref_create__self_reference__rejects(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A document cannot cite itself (422, no row written)."""
    # // given one document
    source_id = _run(_seed_document(sql_session_factory, seed="self-ref"))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when creating a self-reference // then 422 names the target.
        response = client.post(
            f"/api/v1/documents/{source_id}/xrefs",
            json={"target_document_id": str(source_id), "label": "me", "sort_order": 0},
            headers=IDEMPOTENCY,
        )
        assert response.status_code == 422, response.text


def test_doc_ops_router__xref_create__cross_tenant_target__not_found(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A cross-tenant target is indistinguishable from missing (404)."""
    # // given a source in tenant A and a target in tenant B
    source_id = _run(_seed_document(sql_session_factory, seed="x-tenant-src"))
    foreign_id = _run(_seed_document(sql_session_factory, tenant_id=TENANT_B, seed="x-tenant-dst"))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when referencing the foreign document // then 404.
        response = client.post(
            f"/api/v1/documents/{source_id}/xrefs",
            json={"target_document_id": str(foreign_id), "label": "far", "sort_order": 0},
            headers=IDEMPOTENCY,
        )
        assert response.status_code == 404, response.text


def test_doc_ops_router__split_plan__partitions_without_gaps(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A split plan mints output ids + page mapping (T5 separation)."""
    # // given a four-page source document
    source_id = _run(_seed_document(sql_session_factory, seed="split-src"))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when planning a 1..2 / 3..4 split // then the record partitions.
        response = client.post(
            f"/api/v1/documents/{source_id}/separation/split",
            json={
                "case_id": str(CASE_ID),
                "page_count": 4,
                "ranges": [
                    {"start_page": 1, "end_page": 2},
                    {"start_page": 3, "end_page": 4},
                ],
                "reason": "route pages to reviewers",
            },
            headers=IDEMPOTENCY,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["operation"] == "split"
        assert len(body["outputs"]) == 2
        assert body["page_ranges"] == [
            {"start_page": 1, "end_page": 2},
            {"start_page": 3, "end_page": 4},
        ]


def test_doc_ops_router__split_plan__gapped_geometry__rejects(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A gapped split would silently drop pages: 422, nothing planned."""
    # // given a four-page source document
    source_id = _run(_seed_document(sql_session_factory, seed="gap-src"))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when planning ranges that skip page 2 // then 422.
        response = client.post(
            f"/api/v1/documents/{source_id}/separation/split",
            json={
                "case_id": str(CASE_ID),
                "page_count": 4,
                "ranges": [
                    {"start_page": 1, "end_page": 1},
                    {"start_page": 3, "end_page": 4},
                ],
                "reason": "gapped attempt",
            },
            headers=IDEMPOTENCY,
        )
        assert response.status_code == 422, response.text


def test_doc_ops_router__join_merge_plan__concatenates_sources(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A join plan over two sources mints one output (T5 separation)."""
    # // given two source documents in one case
    first_id = _run(_seed_document(sql_session_factory, seed="join-a"))
    second_id = _run(_seed_document(sql_session_factory, seed="join-b"))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when planning a join // then one output id returns.
        response = client.post(
            "/api/v1/documents/separation/join-merge",
            json={
                "case_id": str(CASE_ID),
                "operation": "join",
                "source_document_ids": [str(first_id), str(second_id)],
                "page_counts": [2, 3],
                "reason": "reassemble batch",
            },
            headers=IDEMPOTENCY,
        )
        assert response.status_code == 201, response.text
        assert response.json()["operation"] == "join"
        assert len(response.json()["outputs"]) == 1


def test_doc_ops_router__package__includes_manifest(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Package generation returns a manifest (acceptance artifact)."""
    # // given two documents
    first_id = _run(_seed_document(sql_session_factory, seed="pack-a"))
    second_id = _run(_seed_document(sql_session_factory, seed="pack-b"))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when building a package // then the manifest describes members.
        response = client.post(
            "/api/v1/documents/packages",
            json={
                "name": "Hearing exhibits",
                "member_document_ids": [str(first_id), str(second_id)],
            },
            headers=IDEMPOTENCY,
        )
        assert response.status_code == 201, response.text
        manifest = response.json()["manifest"]
        assert manifest["entry_count"] == 2
        assert "package_sha256" in manifest
        assert {e["document_id"] for e in manifest["entries"]} == {
            str(first_id),
            str(second_id),
        }


def test_doc_ops_router__package__unknown_member__not_found(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unknown member id is 404 (never a partial package)."""
    # // given no documents
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when building over an unknown id // then 404.
        response = client.post(
            "/api/v1/documents/packages",
            json={"name": "Ghost", "member_document_ids": [str(uuid4())]},
            headers=IDEMPOTENCY,
        )
        assert response.status_code == 404, response.text


def test_doc_ops_router__duplicates__shared_hash__groups(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two uploads of the same bytes group by hash (acceptance)."""
    # // given two documents with identical bytes + one distinct
    first_id = _run(_seed_document(sql_session_factory, seed="same-bytes"))
    second_id = _run(_seed_document(sql_session_factory, seed="same-bytes"))
    _run(_seed_document(sql_session_factory, seed="other-bytes"))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when asking for duplicates // then the pair groups.
        response = client.get(f"/api/v1/cases/{CASE_ID}/documents/duplicates")
        assert response.status_code == 200, response.text
        groups = response.json()["groups"]
        assert len(groups) == 1
        assert set(groups[0]) == {str(first_id), str(second_id)}


def test_doc_ops_router__csv_export__authorized_projection(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CSV export renders allowlisted columns with a header row."""
    # // given one internal document
    _run(_seed_document(sql_session_factory, seed="csv-a", file_name="memo.pdf"))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when exporting two columns // then the header + one row return.
        response = client.post(
            f"/api/v1/cases/{CASE_ID}/documents/csv-export",
            json={"columns": ["document_id", "file_name"]},
            headers=IDEMPOTENCY,
        )
        assert response.status_code == 200, response.text
        lines = response.text.splitlines()
        assert lines[0] == "document_id,file_name"
        assert len(lines) == 2
        assert "memo.pdf" in lines[1]


def test_doc_ops_router__csv_export__unknown_column__rejects(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unknown CSV columns are 422 (fail-closed projection)."""
    # // given an app with no documents
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when exporting a non-vocabulary column // then 422.
        response = client.post(
            f"/api/v1/cases/{CASE_ID}/documents/csv-export",
            json={"columns": ["storage_ref"]},
            headers=IDEMPOTENCY,
        )
        assert response.status_code == 422, response.text


def test_doc_ops_router__csv_export__restricted_row__withheld_without_grant(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A reviewer (no read_restricted) never sees restricted rows in CSV."""
    # // given one restricted document
    _run(
        _seed_document(
            sql_session_factory,
            seed="csv-secret",
            file_name="secret.pdf",
            sensitivity=DocumentSensitivity.RESTRICTED,
        )
    )
    app = _build_app(sql_session_factory, roles=("reviewer",))

    with TestClient(app) as client:
        # // when exporting // then the restricted row drops.
        response = client.post(
            f"/api/v1/cases/{CASE_ID}/documents/csv-export",
            json={"columns": ["document_id", "file_name"]},
            headers=IDEMPOTENCY,
        )
        assert response.status_code == 200, response.text
        assert response.text.splitlines() == ["document_id,file_name"]


def test_doc_ops_router__mutation__missing_idempotency_key__rejects(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutations without Idempotency-Key are 400 (FR-005)."""
    # // given one document
    source_id = _run(_seed_document(sql_session_factory, seed="idem-src"))
    target_id = _run(_seed_document(sql_session_factory, seed="idem-dst"))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when posting without the header // then 400, no row written.
        response = client.post(
            f"/api/v1/documents/{source_id}/xrefs",
            json={"target_document_id": str(target_id), "label": "x", "sort_order": 0},
        )
        assert response.status_code == 400, response.text
