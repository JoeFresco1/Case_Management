"""Local filesystem ``DocumentStore`` adapter (P03-US1 / FM-022 / AGENTS.md §12).

# INTEGRATION_SEAM: AzureBlobDocumentStore

The local adapter stores bytes on the filesystem in a content-addressable
layout::

    <documents_path>/<tenant_id>/<sha256[0:2]>/<sha256>

The two-level fan-out keeps any single directory under ~256 entries —
cheap on ext4, cheap on NTFS, and free on any object store we
realistically target.

Why content-addressable: identical bytes from different tenants (or
even the same tenant, two weeks apart) land at the same path. The
storage layer is naturally deduplicated, and a future garbage
collector can walk the tree and reclaim any path that no metadata row
references.

# Path-traversal guard

The ``tenant_id`` is a UUID we built ourselves, so it cannot contain
``..`` or ``/``. ``sha256`` is validated to be 64-char lowercase hex by
``app.domain.document.is_valid_sha256`` before it reaches the
filesystem, so the fan-out segment cannot be a path. Defense in depth:
``put`` re-validates and refuses anything that smells like a traversal
attempt, and ``get`` resolves the path and verifies it is still under
``documents_path`` before reading.

# Failure mode

If the disk is full, the directory is read-only, or the filesystem is
otherwise unwritable, ``put`` raises ``DependencyUnavailable``. The
application use case is responsible for translating the failure to a
``DocumentRejected`` audit event and rolling back the metadata write —
this module never holds the DB transaction, it just stores bytes.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from app.domain.document import is_valid_sha256
from app.domain.errors import DependencyUnavailable


class LocalDocumentStore:
    """Filesystem-backed ``DocumentStore`` (local development adapter).

    Args:
        root: base directory under which bytes are stored. Defaults to
            ``Settings.documents_path``. The directory is created on
            first write; an unwritable root raises ``DependencyUnavailable``
            on the first ``put``.
    """

    def __init__(self, *, root: Path) -> None:
        self._root = Path(root).resolve()

    async def put(
        self,
        *,
        tenant_id: UUID,
        content: bytes,
        content_type: str,
        sha256: str,
    ) -> str:
        """Write ``content`` to ``<root>/<tenant_id>/<sha256[0:2]>/<sha256>``.

        The write is atomic on POSIX (write to a sibling ``.tmp`` file
        then ``os.replace``) so a partial write is never visible to a
        concurrent reader. On Windows ``os.replace`` is also atomic; we
        use the same code path for portability.
        """
        del content_type  # layout is content-addressable, not type-keyed
        if not is_valid_sha256(sha256):
            # A bad digest is a programming error; we refuse to write
            # the bytes at all so a typo cannot land a poisoned file
            # on disk. The application use case computes the digest
            # with ``hashlib.sha256`` before calling ``put``.
            raise ValueError(f"invalid sha256 digest: {sha256!r}")
        path = self._resolve_path(tenant_id, sha256)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            # noqa: ASYNC230 — local FS adapter runs in the FastAPI
            # worker threadpool; streaming via asyncio is reserved for
            # the future AzureBlobDocumentStore adapter.
            with open(tmp, "wb") as f:  # noqa: ASYNC230
                f.write(content)
            os.replace(tmp, path)
        except OSError as exc:
            # Disk full, permission denied, parent unwritable, etc.
            # Surface as the standard adapter-unavailable error so the
            # use case can audit DocumentRejected without leaking the
            # raw errno to the client.
            raise DependencyUnavailable(
                f"document store write failed: {exc.strerror or exc}"
            ) from exc
        # The storage_ref is the path relative to ``self._root``; the
        # metadata row records it verbatim so ``get`` can find the bytes
        # later. Using a relative path means the ``root`` can move
        # without breaking the metadata (e.g. container remount).
        return path.relative_to(self._root).as_posix()

    async def get(self, *, tenant_id: UUID, content_ref: str) -> bytes:
        """Read the bytes previously stored under ``content_ref``.

        Raises:
            DependencyUnavailable: when ``content_ref`` is malformed
                (path traversal) or the file is missing on disk.
        """
        # Earlier Windows uploads persisted native separators. Validate every
        # segment before resolving so a reference cannot cross tenant roots.
        parts = content_ref.replace("\\", "/").split("/")
        digest = parts[-1]
        if len(parts) > 1 and parts != [str(tenant_id), digest[:2], digest]:
            raise DependencyUnavailable("document store ref has an invalid tenant or layout")
        path = self._resolve_path(tenant_id, digest)
        if not path.is_file():
            raise DependencyUnavailable(f"document bytes not found: {content_ref}")
        try:
            # noqa: ASYNC230 — see note in ``put`` above.
            with open(path, "rb") as f:  # noqa: ASYNC230
                return f.read()
        except OSError as exc:
            raise DependencyUnavailable(
                f"document store read failed: {exc.strerror or exc}"
            ) from exc

    # ----- internals --------------------------------------------------------

    def _resolve_path(self, tenant_id: UUID, sha256: str) -> Path:
        """Resolve the absolute path for ``(tenant_id, sha256)``.

        Defense-in-depth: ``tenant_id`` is a UUID (no ``..`` or ``/``),
        and ``sha256`` is restricted to 64-char lowercase hex by
        ``is_valid_sha256``. We verify both here and then assert the
        resolved path is still under ``self._root`` before returning.

        The fan-out is ``<tenant>/<sha[0:2]>/<sha>`` — 256-entry
        directory caps regardless of dataset size, which keeps
        ``readdir`` cheap on every supported filesystem.
        """
        if not is_valid_sha256(sha256):
            raise DependencyUnavailable(f"document store ref is not a valid sha256: {sha256!r}")
        # The tenant id segment is built from a UUID, but defend in
        # depth: refuse anything that looks like a path component.
        tenant_segment = str(tenant_id)
        if "/" in tenant_segment or ".." in tenant_segment:
            raise DependencyUnavailable("document store tenant id contains forbidden characters")
        path = (self._root / tenant_segment / sha256[:2] / sha256).resolve()
        try:
            path.relative_to(self._root.resolve())
        except ValueError as exc:
            # The resolved path escaped the root via a symlink race or
            # similar. Treat as an unavailable adapter so the caller
            # never sees bytes from outside the configured root.
            raise DependencyUnavailable("document store ref escapes the configured root") from exc
        return path


__all__ = ["LocalDocumentStore"]
