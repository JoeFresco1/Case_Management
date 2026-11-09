"""Verify portable references against the actual local document-store adapter."""

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.errors import DependencyUnavailable
from app.infrastructure.storage.local_document_store import LocalDocumentStore


async def test_local_document_store_roundtrip__reads_native_and_portable_refs(
    tmp_path: Path,
) -> None:
    """Exercise a write then read the same bytes through all supported reference layouts."""
    # Given a real store and a synthetic tenant.
    store = LocalDocumentStore(root=tmp_path)
    tenant = uuid4()
    content = b"synthetic file"
    digest = sha256(content).hexdigest()

    # When an upload returns its storage reference.
    ref = await store.put(
        tenant_id=tenant, content=content, content_type="text/plain", sha256=digest
    )

    # Then the issued reference and older Windows and digest-only references resolve.
    assert ref == f"{tenant}/{digest[:2]}/{digest}"
    for stored in (ref, ref.replace("/", "\\"), digest):
        assert await store.get(tenant_id=tenant, content_ref=stored) == content


async def test_local_document_store_invalid_ref__denies_other_tenant_and_traversal(
    tmp_path: Path,
) -> None:
    """Reject mismatching tenant, shard and path segments even when valid bytes exist."""
    # Given identical bytes in both tenant roots.
    store = LocalDocumentStore(root=tmp_path)
    tenant, other = uuid4(), uuid4()
    content = b"synthetic file"
    digest = sha256(content).hexdigest()
    for owner in (tenant, other):
        await store.put(tenant_id=owner, content=content, content_type="text/plain", sha256=digest)

    # When a caller substitutes a tenant, shard or traversal segment, then access fails.
    for ref in (f"{other}/{digest[:2]}/{digest}", f"{tenant}/zz/{digest}", f"../{digest}"):
        with pytest.raises(DependencyUnavailable):
            await store.get(tenant_id=tenant, content_ref=ref)
