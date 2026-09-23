"""Add a new version of an existing document (P03-US2 / FM-023 / FR-006).

Workers add a new bytes-only revision of a document that already
exists on a case. The flow:

1. **Authorize** the actor against ``Action.DOCUMENT_UPLOAD`` on the
   case's tenant (P11-US2, FM-002). The add-version operation reuses
   the same permission as the initial upload because it is the same
   write-side operation; only the metadata pointer changes.
2. **Resolve the document** within the actor's tenant. A cross-tenant
   document id is surfaced as ``NotFound`` so we never confirm the
   existence of a document in another tenant.
3. **Validate** the new content against the case type's policy:
   - ``content_type`` must be in the case type's
     ``document_allowed_content_types`` (default: any).
   - ``size`` must be <= the case type's ``document_max_size_bytes``
     (default: 25 MiB).
   The classification is fixed by the existing document row — a new
   version does not reclassify the parent (P03 pillar Q2 — going
   forward only; historical metadata preserved).
4. **Hash** the content with SHA-256 so identical bytes land at the
   same storage path and the version row records the digest.
5. **Store bytes** through ``DocumentStore.put``. A storage failure
   raises ``DependencyUnavailable`` and rolls the transaction back; a
   successful write returns the ``storage_ref``.
6. **Bump the aggregate**: ``Document.with_new_version`` returns a
   new ``Document`` whose ``current_version`` pointer is incremented
   and whose ``sha256``/``size``/``storage_ref`` reflect the latest
   bytes. Prior version rows in ``document_versions`` are untouched.
7. **Persist** the new ``DocumentVersion`` row, the updated
   ``Document`` row, the ``VersionAdded`` audit event, and the
   ``VersionAdded`` outbox event in the same transaction (one
   ``add_version`` call on the repository performs the version insert
   + the metadata update atomically).

# INTEGRATION_SEAM: DocumentStore

Same seam as ``upload_document``: the local FS adapter is wired in
production today; Azure Blob is the future seam
(``docs/integrations/seams.md``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.documents.upload_document import (
    DEFAULT_MAX_SIZE_BYTES,
    SUBJECT_TYPE_DOCUMENT,
    _policy_from,
    _UserProxy,
)
from app.domain.case import Case
from app.domain.case_type import CaseTypeVersion
from app.domain.document import Document, DocumentVersion
from app.domain.errors import DocumentLocked, NotFound, ValidationFailed
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    DocumentRepository,
    DocumentStore,
    OutboxWriter,
)

# Stable event types. Subscribers depend on these literals.
VERSION_ADDED = "VersionAdded"


@dataclass(frozen=True, slots=True)
class VersionDocumentCommand:
    """The input to ``version_document.handle``.

    ``document_id`` identifies the existing document the new bytes
    belong to. The case id is resolved through the document row; the
    HTTP layer only carries the document id in the URL.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID
    content: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class VersionDocumentResult:
    """The outcome of a successful version add.

    ``document`` reflects the post-update aggregate (its
    ``current_version`` pointer has been bumped); ``version`` is the
    freshly persisted ``DocumentVersion`` row.
    """

    document: Document
    version: DocumentVersion


async def handle(
    cmd: VersionDocumentCommand,
    *,
    case_repo: CaseRepository,
    case_type_version_repo: Any,
    document_repo: DocumentRepository,
    document_store: DocumentStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> VersionDocumentResult:
    """Append a new version to an existing document atomically.

    Failure behavior:
    - Auth / not-found / validation errors raise before any DB write.
    - On ``DocumentStore.put`` raising ``DependencyUnavailable`` the
      caller is expected to roll the transaction back; no audit row
      is appended in that case because there is no durable domain
      change.
    """
    # 1. Authorization. The same Action as the initial upload — adding
    # a version is the same write-side capability. The resource kind
    # is "case" but we don't yet know the case_id; the policy reads
    # only tenant_id + roles + is_system so we pass a placeholder
    # resource; the per-document tenant filter at the repository is the
    # binding tenant check.
    await authorize(
        Action.DOCUMENT_UPLOAD,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
    )

    # 2. Resolve the document within the actor's tenant. The repository
    # MUST filter by tenant_id so a cross-tenant document id surfaces
    # as NotFound rather than a leak.
    document: Document | None = await document_repo.get(cmd.tenant_id, cmd.document_id)
    if document is None:
        raise NotFound(f"document {cmd.document_id} not found")

    # 3. Resolve the case (for the audit case_id) + case-type version
    # (for the upload policy). The policy is inherited from the case
    # type that the parent case was created under; revisions of the
    # case-type policy do not retroactively tighten an existing
    # document's rules (P03 pillar Q2 — going forward only). We
    # still read the live version because the parent case references
    # it directly and reading a missing version surfaces a 404 fast.
    case: Case | None = await case_repo.get(cmd.tenant_id, document.case_id)
    if case is None:
        # The case row was deleted under us — treat the document as
        # orphaned and surface 404. The audit row for the case link
        # is preserved by the existing document row.
        raise NotFound(f"case {document.case_id} not found")

    version_row: CaseTypeVersion | None = await case_type_version_repo.get(
        cmd.tenant_id, case.case_type_version_id
    )
    if version_row is None:
        raise NotFound(f"case type version {case.case_type_version_id} not found")

    # 4. Policy validation. Classification is fixed by the existing
    # document row and is NOT re-validated here (P03 pillar Q2). The
    # size + content-type checks still apply.
    _validate_against_policy(cmd, version_row)

    # 4b. Lock check (P03-US8). Locked documents refuse new versions
    # BEFORE bytes are stored so a locked document cannot be silently
    # overwritten with a write that is then rolled back. The check is
    # symmetric with ``protect_document.redact``.
    if document.locked:
        raise DocumentLocked(f"document {document.id} is locked; new versions are refused")

    # 5. Hash content. Same canonical hash as the upload use case.
    sha256 = _sha256_hex(cmd.content)
    size = len(cmd.content)

    # 6. Store bytes through the port.
    storage_ref = await document_store.put(
        tenant_id=cmd.tenant_id,
        content=cmd.content,
        content_type=cmd.content_type,
        sha256=sha256,
    )

    # 7. Build the next aggregate + version row. ``with_new_version``
    # bumps the pointer; the prior ``DocumentVersion`` rows are
    # preserved (P03-US2 acceptance scenario C5).
    now = clock.now()
    next_document = document.with_new_version(
        new_sha256=sha256,
        new_size=size,
        new_storage_ref=storage_ref,
        now=now,
    )
    new_version = DocumentVersion(
        id=uuid4(),
        tenant_id=cmd.tenant_id,
        document_id=document.id,
        version_number=next_document.current_version,
        sha256=sha256,
        size=size,
        storage_ref=storage_ref,
        content_type=cmd.content_type,
        uploaded_by=cmd.actor_id,
        uploaded_at=now,
    )

    correlation_id = _new_correlation_id()

    # Single repository call stages the version-row insert AND the
    # metadata-pointer update on the same session so the transaction
    # commits once (atomicity guarantee for FM-023).
    await document_repo.add_version(document=next_document, version=new_version)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=VERSION_ADDED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DOCUMENT,
        subject_id=document.id,
        case_id=case.id,
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            "case_id": str(case.id),
            "document_id": str(document.id),
            "file_name": document.file_name,
            "classification": document.classification,
            "sensitivity": document.sensitivity.value,
            "new_version_number": new_version.version_number,
            "previous_version_number": document.current_version,
            "sha256": new_version.sha256,
            "size": new_version.size,
            "content_type": new_version.content_type,
            "storage_ref": new_version.storage_ref,
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=VERSION_ADDED,
        event_id=uuid4(),
        payload={
            "case_id": str(case.id),
            "document_id": str(document.id),
            "version_id": str(new_version.id),
            "version_number": new_version.version_number,
            "sha256": sha256,
            "size": size,
            "content_type": cmd.content_type,
            "occurred_at": now.isoformat(),
        },
    )

    return VersionDocumentResult(document=next_document, version=new_version)


# ---------------------------------------------------------------------------
# Helpers (local — kept small and self-contained)
# ---------------------------------------------------------------------------


def _validate_against_policy(cmd: VersionDocumentCommand, version: CaseTypeVersion) -> None:
    """Apply the case-type upload policy's size + content-type checks.

    The classification is inherited from the existing document row and
    is not re-validated here (P03 pillar Q2 — going forward only).
    """
    policy = _policy_from(version)
    allowed_types = policy["allowed_content_types"]
    if allowed_types and cmd.content_type not in allowed_types:
        raise ValidationFailed(
            f"content type {cmd.content_type!r} is not permitted for this case type",
            field="content_type",
        )
    max_size = policy["max_size_bytes"] or DEFAULT_MAX_SIZE_BYTES
    if max_size and len(cmd.content) > max_size:
        raise ValidationFailed(
            f"document exceeds configured max size ({max_size} bytes)",
            field="size",
        )


def _sha256_hex(content: bytes) -> str:
    """SHA-256 of ``content`` as lowercase hex (canonical hashlib shape)."""
    return hashlib.sha256(content).hexdigest()


def _user_context(cmd: VersionDocumentCommand) -> Any:
    """Duck-typed ``UserContext`` for the policy (mirrors upload)."""
    return _UserProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


def _new_correlation_id() -> str:
    """Return a fresh uuid7 string for the audit/outbox correlation id."""
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


__all__ = [
    "VERSION_ADDED",
    "VersionDocumentCommand",
    "VersionDocumentResult",
    "handle",
]
