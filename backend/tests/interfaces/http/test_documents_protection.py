"""HTTP acceptance for FM-025 document protection (T5).

Proves the router contract end-to-end with dependency overrides:

- guessed-URL ``GET /documents/{id}/content`` for a restricted
  document returns 403 with a metadata-free reason (no file name,
  storage ref, or bytes in the body);
- a cross-tenant id returns 404 (no existence oracle);
- ``POST /documents/{id}/restrict`` flips the label and a subsequent
  guessed-URL download is denied.

Naming follows ``test_<unit>_<scenario>__<expectation>`` (AGENTS.md
§4.R5). In-memory fakes only; no container.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import CorrelationContext, UserContext, get_clock, get_correlation_id
from app.deps import get_current_user as _get_current_user
from app.deps import get_db as _get_db
from app.domain.document import Document, DocumentSensitivity, DocumentVersion
from app.domain.errors import DomainError
from app.interfaces.http.dependencies import get_case_dependencies
from app.interfaces.http.errors import domain_error_handler
from app.interfaces.http.routers.documents import (
    get_document_store,
    versions_router,
)
from tests.application.cases._fakes import FixedClock, InMemoryAuditStore, InMemoryOutbox
from tests.application.documents._fakes import (
    InMemoryDocumentRepository,
    InMemoryDocumentStore,
)

TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
SUPERVISOR = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
REVIEWER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CASE_A = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
NOW = datetime(2026, 9, 1, tzinfo=UTC)


class _FakeSession:
    """Minimal async session: commit/rollback are no-ops for fakes."""

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _document(*, sensitivity: DocumentSensitivity = DocumentSensitivity.INTERNAL) -> Document:
    content = b"%PDF-1.4\nsensitive bytes"
    sha = hashlib.sha256(content).hexdigest()
    return Document.create(
        tenant_id=TENANT_A,
        case_id=CASE_A,
        file_name="onboarding.pdf",
        content_type="application/pdf",
        classification="Contract",
        sensitivity=sensitivity,
        sha256=sha,
        size=len(content),
        storage_ref=f"{TENANT_A}/{sha[:2]}/{sha}",
        uploaded_by=SUPERVISOR,
        now=NOW,
    )


def _make_client(
    *,
    actor_id: UUID,
    actor_roles: tuple[str, ...],
    tenant_id: UUID,
    repo: InMemoryDocumentRepository,
    store: InMemoryDocumentStore,
    audit: InMemoryAuditStore,
) -> TestClient:
    """Build a TestClient with all document-router dependencies overridden."""
    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(versions_router, prefix="/api/v1")
    outbox = InMemoryOutbox()

    def _provider() -> Any:
        class _P:
            def for_session(self, _session: object) -> Any:
                return SimpleNamespace(document=repo, audit=audit, outbox=outbox)

        return _P()

    app.dependency_overrides[get_case_dependencies] = _provider
    app.dependency_overrides[_get_current_user] = lambda: UserContext(
        user_id=actor_id,
        tenant_id=tenant_id,
        display_name="",
        roles=actor_roles,
        is_system=False,
    )
    app.dependency_overrides[_get_db] = _FakeSession
    app.dependency_overrides[get_clock] = lambda: FixedClock(NOW)
    app.dependency_overrides[get_correlation_id] = lambda: CorrelationContext(
        correlation_id="corr-test"
    )
    app.dependency_overrides[get_document_store] = lambda: store
    return TestClient(app, raise_server_exceptions=False)


def _seed_repo(
    sensitivity: DocumentSensitivity = DocumentSensitivity.INTERNAL,
) -> tuple[Document, InMemoryDocumentRepository, InMemoryDocumentStore, InMemoryAuditStore]:
    doc = _document(sensitivity=sensitivity)
    content = b"%PDF-1.4\nsensitive bytes"
    repo = InMemoryDocumentRepository()
    version = DocumentVersion(
        id=UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        tenant_id=TENANT_A,
        document_id=doc.id,
        version_number=1,
        sha256=doc.sha256,
        size=doc.size,
        storage_ref=doc.storage_ref,
        content_type=doc.content_type,
        uploaded_by=SUPERVISOR,
        uploaded_at=NOW,
    )
    repo.added.append((doc, version))
    repo._docs[(TENANT_A, doc.id)] = doc  # noqa: SLF001
    repo._versions.setdefault((TENANT_A, doc.id), []).append(version)  # noqa: SLF001
    store = InMemoryDocumentStore()
    store._by_ref[(TENANT_A, doc.storage_ref)] = content  # noqa: SLF001
    return doc, repo, store, InMemoryAuditStore()


def test_download_document__restricted_guessed_url__returns_403_without_leak() -> None:
    # // given a restricted document in tenant A
    doc, repo, store, audit = _seed_repo(DocumentSensitivity.RESTRICTED)
    # // when a view-only reviewer guesses the URL
    client = _make_client(
        actor_id=REVIEWER,
        actor_roles=("reviewer",),
        tenant_id=TENANT_A,
        repo=repo,
        store=store,
        audit=audit,
    )
    response = client.get(f"/api/v1/documents/{doc.id}/content")
    # // then 403 with a safe reason only — no file name, ref, or bytes
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "forbidden"
    blob = response.text
    assert "onboarding.pdf" not in blob
    assert doc.storage_ref not in blob
    assert "sensitive bytes" not in blob


def test_download_document__cross_tenant_guessed_url__returns_404() -> None:
    # // given a document owned by tenant A
    doc, repo, store, audit = _seed_repo()
    # // when tenant B guesses the id
    client = _make_client(
        actor_id=SUPERVISOR,
        actor_roles=("supervisor",),
        tenant_id=TENANT_B,
        repo=repo,
        store=store,
        audit=audit,
    )
    response = client.get(f"/api/v1/documents/{doc.id}/content")
    # // then 404 (no existence oracle across tenants)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_restrict_document__then_guessed_url_denied__locks_down_reads() -> None:
    # // given an internal document
    doc, repo, store, audit = _seed_repo(DocumentSensitivity.INTERNAL)
    supervisor_client = _make_client(
        actor_id=SUPERVISOR,
        actor_roles=("supervisor",),
        tenant_id=TENANT_A,
        repo=repo,
        store=store,
        audit=audit,
    )
    # // when a supervisor restricts it
    restrict = supervisor_client.post(
        f"/api/v1/documents/{doc.id}/restrict",
        json={"sensitivity": "restricted"},
        headers={"Idempotency-Key": "key-1"},
    )
    assert restrict.status_code == 200
    assert restrict.json()["sensitivity"] == "restricted"
    # // then the view-only reviewer's guessed-URL download is denied
    reviewer_client = _make_client(
        actor_id=REVIEWER,
        actor_roles=("reviewer",),
        tenant_id=TENANT_A,
        repo=repo,
        store=store,
        audit=InMemoryAuditStore(),
    )
    denied = reviewer_client.get(f"/api/v1/documents/{doc.id}/content")
    assert denied.status_code == 403
    # // ... while the supervisor still reads the original
    allowed = supervisor_client.get(f"/api/v1/documents/{doc.id}/content")
    assert allowed.status_code == 200
