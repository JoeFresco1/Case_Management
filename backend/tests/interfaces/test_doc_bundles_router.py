"""SQLite endpoint coverage for permissioned bundles + share-by-link (T5-US3).

Exercises the production provider composition
(``SqlCaseHttpDependencyProvider``) with process-local bundle/link
stores end-to-end:

- bundle lifecycle: create → grant → member change → revoke (frozen
  membership, dead links);
- share-by-link: mint (one-time raw token) → redeem (use counting) →
  exhaustion → revocation → unknown-token denial shape;
- isolation acceptance: "bundle content not searchable by content by
  non-members" — a bundle grantee without ``document.read`` gets 403
  (metadata-free) on the bundle read path for strangers, on the
  content-search/duplicates path, and on the content-download path.
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

from app.application.documents.doc_ops import (
    InMemoryBundleStore,
    InMemoryLinkStore,
)
from app.deps import UserContext, get_current_user, get_db
from app.domain.document import Document, DocumentSensitivity, DocumentVersion
from app.domain.errors import DomainError
from app.infrastructure.persistence.case_http_dependencies import (
    SqlCaseHttpDependencyProvider,
)
from app.infrastructure.persistence.documents import SqlDocumentRepository
from app.infrastructure.storage.local_document_store import LocalDocumentStore
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.doc_bundles import (
    get_bundle_store,
    get_link_store,
)
from app.interfaces.http.routers.doc_bundles import (
    router as bundles_router,
)
from app.interfaces.http.routers.doc_ops import router as doc_ops_router
from app.interfaces.http.routers.documents import (
    get_document_store,
)
from app.interfaces.http.routers.documents import (
    versions_router as documents_versions_router,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
OWNER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
GRANTEE_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
STRANGER_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
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
    seed: str,
    file_name: str = "exhibit.pdf",
    content: bytes | None = None,
) -> UUID:
    """Stage one document + version row (+ bytes when given); return its id."""
    payload = content if content is not None else seed.encode("utf-8")
    document = Document.create(
        tenant_id=TENANT_A,
        case_id=CASE_ID,
        file_name=file_name,
        content_type="application/pdf",
        classification="Evidence",
        sensitivity=DocumentSensitivity.INTERNAL,
        sha256=_sha(seed),
        size=len(payload),
        storage_ref=f"store/{seed}",
        uploaded_by=OWNER_ID,
        now=NOW,
    )
    version = DocumentVersion(
        id=uuid4(),
        tenant_id=TENANT_A,
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
    bundle_store: InMemoryBundleStore | None = None,
    link_store: InMemoryLinkStore | None = None,
    document_store: LocalDocumentStore | None = None,
) -> FastAPI:
    """Build a minimal app using the production provider composition."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.state.case_http_dependencies = SqlCaseHttpDependencyProvider()
    app.state.bundle_store = bundle_store or InMemoryBundleStore()
    app.state.link_store = link_store or InMemoryLinkStore()
    app.state.document_store = document_store or LocalDocumentStore(root="/tmp/doc-ops-test-store")
    app.include_router(bundles_router, prefix="/api/v1")
    app.include_router(doc_ops_router, prefix="/api/v1")
    app.include_router(documents_versions_router, prefix="/api/v1")
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        display_name="Synthetic User",
        roles=roles,
    )
    app.dependency_overrides[get_bundle_store] = lambda: app.state.bundle_store
    app.dependency_overrides[get_link_store] = lambda: app.state.link_store
    app.dependency_overrides[get_document_store] = lambda: app.state.document_store
    return app


def _mint(
    client: TestClient, bundle_id: str, *, ttl_seconds: int = 3600, max_uses: int = 3
) -> dict[str, Any]:
    """Mint a share link; return the decoded mint response."""
    response = client.post(
        f"/api/v1/bundles/{bundle_id}/links",
        json={"ttl_seconds": ttl_seconds, "max_uses": max_uses},
        headers={**IDEMPOTENCY, "Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def test_bundles_router__create_grant_change__round_trips(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Owner creates a bundle, grants viewer, edits members (T5-US3)."""
    # // given two documents
    first_id = _run(_seed_document(sql_session_factory, seed="bundle-a"))
    second_id = _run(_seed_document(sql_session_factory, seed="bundle-b"))
    app = _build_app(sql_session_factory)

    with TestClient(app) as client:
        # // when creating a bundle // then metadata returns ACTIVE.
        created = client.post(
            "/api/v1/bundles",
            json={"name": "Hearing exhibits", "member_document_ids": [str(first_id)]},
            headers=IDEMPOTENCY,
        )
        assert created.status_code == 201, created.text
        bundle_id = created.json()["id"]
        assert created.json()["status"] == "ACTIVE"

        # // when granting viewer to the grantee // then 200.
        granted = client.post(
            f"/api/v1/bundles/{bundle_id}/grants",
            json={"grantee_id": str(GRANTEE_ID), "role": "viewer"},
            headers={**IDEMPOTENCY, "Idempotency-Key": str(uuid4())},
        )
        assert granted.status_code == 200, granted.text

        # // when adding the second member // then both members list.
        changed = client.patch(
            f"/api/v1/bundles/{bundle_id}/members",
            json={"added": [str(second_id)], "removed": []},
            headers={**IDEMPOTENCY, "Idempotency-Key": str(uuid4())},
        )
        assert changed.status_code == 200, changed.text
        assert set(changed.json()["member_document_ids"]) == {
            str(first_id),
            str(second_id),
        }


def test_bundles_router__grantee__reads_but_cannot_edit(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Viewer opens the bundle read path but not membership edits."""
    # // given a bundle with a viewer grant
    first_id = _run(_seed_document(sql_session_factory, seed="viewer-a"))
    bundle_store = InMemoryBundleStore()
    link_store = InMemoryLinkStore()
    owner_app = _build_app(sql_session_factory, bundle_store=bundle_store, link_store=link_store)
    with TestClient(owner_app) as client:
        created = client.post(
            "/api/v1/bundles",
            json={"name": "Shared", "member_document_ids": [str(first_id)]},
            headers=IDEMPOTENCY,
        )
        assert created.status_code == 201, created.text
        bundle_id = created.json()["id"]
        granted = client.post(
            f"/api/v1/bundles/{bundle_id}/grants",
            json={"grantee_id": str(GRANTEE_ID), "role": "viewer"},
            headers={**IDEMPOTENCY, "Idempotency-Key": str(uuid4())},
        )
        assert granted.status_code == 200, granted.text

    # // when the grantee reads // then metadata returns.
    grantee_app = _build_app(
        sql_session_factory,
        roles=("reviewer",),
        user_id=GRANTEE_ID,
        bundle_store=bundle_store,
        link_store=link_store,
    )
    with TestClient(grantee_app) as client:
        read = client.get(f"/api/v1/bundles/{bundle_id}")
        assert read.status_code == 200, read.text
        assert read.json()["member_document_ids"] == [str(first_id)]
        # // when the grantee edits // then 403 (viewer != editor).
        changed = client.patch(
            f"/api/v1/bundles/{bundle_id}/members",
            json={"added": [], "removed": [str(first_id)]},
            headers=IDEMPOTENCY,
        )
        assert changed.status_code == 403, changed.text


def test_bundles_router__stranger__denies_without_metadata(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An ungranted actor learns nothing about the bundle (no leak)."""
    # // given a bundle the stranger cannot see
    first_id = _run(_seed_document(sql_session_factory, seed="hidden-a"))
    bundle_store = InMemoryBundleStore()
    link_store = InMemoryLinkStore()
    owner_app = _build_app(sql_session_factory, bundle_store=bundle_store, link_store=link_store)
    with TestClient(owner_app) as client:
        created = client.post(
            "/api/v1/bundles",
            json={"name": "Secret exhibits", "member_document_ids": [str(first_id)]},
            headers=IDEMPOTENCY,
        )
        assert created.status_code == 201, created.text
        bundle_id = created.json()["id"]

    # // when the stranger reads // then 403 with a bare reason only.
    stranger_app = _build_app(
        sql_session_factory,
        roles=("reviewer",),
        user_id=STRANGER_ID,
        bundle_store=bundle_store,
        link_store=link_store,
    )
    with TestClient(stranger_app) as client:
        read = client.get(f"/api/v1/bundles/{bundle_id}")
        assert read.status_code == 403, read.text
        assert "Secret exhibits" not in read.text


def test_bundles_router__link__redeems_then_exhausts(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A single-use link redeems once; the replay is exhausted (403)."""
    # // given a bundle plus a single-use link
    first_id = _run(_seed_document(sql_session_factory, seed="link-a"))
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/bundles",
            json={"name": "Linked", "member_document_ids": [str(first_id)]},
            headers=IDEMPOTENCY,
        )
        assert created.status_code == 201, created.text
        minted = _mint(client, created.json()["id"], max_uses=1)
        # // the raw token appears exactly once in the mint response.
        assert len(minted["raw_token"]) >= 16

        # // when redeeming // then the member list returns (no bytes).
        redeemed = client.post(
            "/api/v1/bundles/links/redeem", json={"raw_token": minted["raw_token"]}
        )
        assert redeemed.status_code == 200, redeemed.text
        assert redeemed.json()["member_document_ids"] == [str(first_id)]

        # // when redeeming again // then 403 exhausted, no metadata.
        replay = client.post(
            "/api/v1/bundles/links/redeem", json={"raw_token": minted["raw_token"]}
        )
        assert replay.status_code == 403, replay.text
        assert "exhausted" in replay.text


def test_bundles_router__link__unknown_token__denies_without_signal(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A guessed token denies indistinguishably (no existence signal)."""
    # // given no stored link for the token
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        # // when redeeming a stranger token // then 403 unknown only.
        response = client.post(
            "/api/v1/bundles/links/redeem",
            json={"raw_token": "guessed-token-value-00000000000000"},
        )
        assert response.status_code == 403, response.text
        assert "unknown" in response.text


def test_bundles_router__revoke__kills_bundle_and_links(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Revoking the bundle freezes members and kills bound links."""
    # // given a bundle with a live link
    first_id = _run(_seed_document(sql_session_factory, seed="revoke-a"))
    app = _build_app(sql_session_factory)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/bundles",
            json={"name": "Doomed", "member_document_ids": [str(first_id)]},
            headers=IDEMPOTENCY,
        )
        assert created.status_code == 201, created.text
        bundle_id = created.json()["id"]
        minted = _mint(client, bundle_id)

        # // when revoking the bundle // then status flips to REVOKED.
        revoked = client.post(
            f"/api/v1/bundles/{bundle_id}/revoke",
            json={"reason": "matter closed"},
            headers={**IDEMPOTENCY, "Idempotency-Key": str(uuid4())},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "REVOKED"

        # // then the bound link dies implicitly (revoked, not expired).
        dead = client.post("/api/v1/bundles/links/redeem", json={"raw_token": minted["raw_token"]})
        assert dead.status_code == 403, dead.text
        assert "revoked" in dead.text

        # // and membership edits deny (revoked bundles refuse all paths).
        frozen = client.patch(
            f"/api/v1/bundles/{bundle_id}/members",
            json={"added": [], "removed": [str(first_id)]},
            headers={**IDEMPOTENCY, "Idempotency-Key": str(uuid4())},
        )
        assert frozen.status_code == 403, frozen.text
        assert "revoked" in frozen.text


def test_bundles_router__non_member__cannot_search_or_download_content(
    sql_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bundle membership never makes bytes searchable by non-members.

    Acceptance: "bundle content not searchable by content by
    non-members". A bundle grantee whose role lacks ``document.read``
    (``intake``) holds a valid bundle grant yet is denied on every
    content path: the duplicates/search surface and the byte download.
    """
    # // given a document in a bundle granted to an intake-role actor
    first_id = _run(_seed_document(sql_session_factory, seed="fenced-bytes"))
    bundle_store = InMemoryBundleStore()
    link_store = InMemoryLinkStore()
    owner_app = _build_app(sql_session_factory, bundle_store=bundle_store, link_store=link_store)
    with TestClient(owner_app) as client:
        created = client.post(
            "/api/v1/bundles",
            json={"name": "Fenced", "member_document_ids": [str(first_id)]},
            headers=IDEMPOTENCY,
        )
        assert created.status_code == 201, created.text
        bundle_id = created.json()["id"]
        granted = client.post(
            f"/api/v1/bundles/{bundle_id}/grants",
            json={"grantee_id": str(GRANTEE_ID), "role": "viewer"},
            headers={**IDEMPOTENCY, "Idempotency-Key": str(uuid4())},
        )
        assert granted.status_code == 200, granted.text

    # // when the grantee (intake: no document.read) searches content
    # // then 403 on the duplicates surface ...
    fenced_app = _build_app(
        sql_session_factory,
        roles=("intake",),
        user_id=GRANTEE_ID,
        bundle_store=bundle_store,
        link_store=link_store,
    )
    with TestClient(fenced_app) as client:
        search = client.get(f"/api/v1/cases/{CASE_ID}/documents/duplicates")
        assert search.status_code == 403, search.text
        # // ... and 403 on the byte download (guessed URL learns nothing).
        download = client.get(f"/api/v1/documents/{first_id}/content")
        assert download.status_code == 403, download.text
