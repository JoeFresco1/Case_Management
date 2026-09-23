"""Restrict access to documents via redaction + lock (P03-US6, US8 / FM-025).

Two operations share this module:

1. **Redact** (``redact_document.handle``) — a worker creates a new
   versioned redaction that points at the bytes they are redacting
   away from. The redacted bytes are written through ``DocumentStore.put``
   (content-addressable) and a ``document_redactions`` row is appended.
   The original ``Document`` aggregate and its ``DocumentVersion`` rows
   are untouched so the original bytes remain retrievable for audit
   replay. A prior ``ACTIVE`` redaction (if any) is marked
   ``SUPERSEDED`` in the same transaction so the timeline always shows
   exactly one ACTIVE redaction per document at any time.

2. **Lock** (``lock_document.handle``) — a worker locks the document so
   no further mutations (new versions, new redactions) are accepted.
   Locking is irreversible in the first slice; the operation is
   idempotent in that a re-lock raises ``DocumentLocked`` so a
   misconfigured client cannot silently re-lock.

Both operations run in the same DB transaction as the audit + outbox
rows so a partial commit cannot leave a redaction visible without an
audit trail.

# INTEGRATION_SEAM: DocumentStore

Same seam as the upload / version flows: the local filesystem adapter
is wired in production today; Azure Blob is the future seam
(``docs/integrations/seams.md``).

# Failure semantics

- Auth / not-found / validation errors raise before any DB write.
- Lock refusal (``DocumentLocked``) raises before any DB write.
- On ``DocumentStore.put`` raising ``DependencyUnavailable`` the caller
  is expected to roll the transaction back; no audit row is appended
  because there is no documented domain change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.documents.upload_document import (
    SUBJECT_TYPE_DOCUMENT,
    _UserProxy,
)
from app.domain.document import Document, DocumentRedaction
from app.domain.errors import DocumentLocked, NotFound, ValidationFailed
from app.domain.ports import (
    AuditEventStore,
    DocumentRepository,
    DocumentStore,
    OutboxWriter,
)

# Stable event types. Subscribers depend on these literals.
REDACTION_CREATED = "DocumentRedacted"
DOCUMENT_LOCKED = "DocumentLocked"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RedactDocumentCommand:
    """The input to ``redact_document.handle``.

    ``content`` is the redacted bytes (e.g. a PDF with sensitive
    regions blacked out). The use case writes them through
    ``DocumentStore`` and inserts a ``document_redactions`` row.
    ``base_version_number`` records which version of the document the
    redaction was derived from so the audit can prove the lineage.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID
    base_version_number: int
    content: bytes
    content_type: str
    reason: str
    region: str | None = None


@dataclass(frozen=True, slots=True)
class RedactDocumentResult:
    """The outcome of a successful redaction.

    ``redaction`` is the freshly persisted ``DocumentRedaction`` row;
    ``document`` is the unchanged parent aggregate (the original bytes
    are still retrievable through the original ``DocumentVersion``).
    """

    redaction: DocumentRedaction
    document: Document


async def redact(
    cmd: RedactDocumentCommand,
    *,
    document_repo: DocumentRepository,
    document_store: DocumentStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> RedactDocumentResult:
    """Append a versioned redaction while preserving the original.

    Steps:

    1. Authorize ``Action.DOCUMENT_REDACT`` on the document's tenant.
    2. Resolve the document within the actor's tenant; cross-tenant
       ids surface as ``NotFound`` (Constitution V).
    3. Lock check: a locked document refuses redactions (P03-US8).
       The check is intentional — redactions on a locked document are
       not silently dropped, they raise so the caller can audit the
       refusal.
    4. Validate ``reason`` is non-empty and ``base_version_number``
       is a known version of the document. ``version <= 0`` or
       ``version > document.current_version`` is rejected.
    5. Hash + write the redacted bytes through ``DocumentStore.put``.
    6. Build the new ``DocumentRedaction`` row (status=ACTIVE) and
       insert it via ``DocumentRepository.add_redaction`` (which
       supersedes the prior ACTIVE row in the same transaction).
    7. Append the audit row + outbox row in the same transaction.

    The original ``Document`` aggregate and ``DocumentVersion`` rows
    are NOT modified; ``add_redaction`` only touches the new
    ``document_redactions`` row + the prior ACTIVE row's status.
    """
    # 1. Authorization.
    await authorize(
        Action.DOCUMENT_REDACT,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
    )

    # 2. Resolve within the actor's tenant.
    document: Document | None = await document_repo.get(cmd.tenant_id, cmd.document_id)
    if document is None:
        raise NotFound(f"document {cmd.document_id} not found")

    # 3. Lock check (P03-US8). The lock check is symmetric with
    # ``Document.with_new_version``; a locked document refuses any
    # write-side operation so the audit timeline stays consistent.
    if document.locked:
        raise DocumentLocked(f"document {document.id} is locked; redactions are refused")

    # 4. Validate the base version. The use case must verify the
    # base_version_number exists in this document's history; otherwise
    # the audit trail could record "redacted from v3" on a document
    # that only has v1.
    if cmd.base_version_number < 1 or cmd.base_version_number > document.current_version:
        raise ValidationFailed(
            f"base_version_number {cmd.base_version_number} is not in the "
            f"document's version history (1..{document.current_version})",
            field="base_version_number",
        )
    if not cmd.reason or not cmd.reason.strip():
        raise ValidationFailed(
            "redaction reason is required",
            field="reason",
        )

    # 5. Hash + write redacted bytes. Storage failures propagate as
    # ``DependencyUnavailable``; the use case boundary rolls the
    # transaction back without writing the audit row.
    sha256 = _sha256_hex(cmd.content)
    size = len(cmd.content)
    storage_ref = await document_store.put(
        tenant_id=cmd.tenant_id,
        content=cmd.content,
        content_type=cmd.content_type,
        sha256=sha256,
    )

    # 6. Build the new redaction row + insert + supersede prior.
    now = clock.now()
    redaction = DocumentRedaction.create(
        tenant_id=cmd.tenant_id,
        document_id=document.id,
        base_version_number=cmd.base_version_number,
        reason=cmd.reason.strip(),
        sha256=sha256,
        size=size,
        storage_ref=storage_ref,
        content_type=cmd.content_type,
        region=cmd.region.strip() if cmd.region else None,
        actor_id=cmd.actor_id,
        now=now,
    )
    await document_repo.add_redaction(redaction=redaction, supersede_prior=True)

    correlation_id = _new_correlation_id()

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=REDACTION_CREATED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DOCUMENT,
        subject_id=document.id,
        case_id=document.case_id,
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            "file_name": document.file_name,
            "classification": document.classification,
            "redaction_id": str(redaction.id),
            "base_version_number": cmd.base_version_number,
            "sha256": sha256,
            "size": size,
            "content_type": cmd.content_type,
            "storage_ref": storage_ref,
            "reason": redaction.reason,
            "region": redaction.region,
            "original_sha256": document.sha256,
            "original_size": document.size,
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=REDACTION_CREATED,
        event_id=uuid4(),
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            "redaction_id": str(redaction.id),
            "base_version_number": cmd.base_version_number,
            "sha256": sha256,
            "size": size,
            "content_type": cmd.content_type,
            "occurred_at": now.isoformat(),
        },
    )

    return RedactDocumentResult(redaction=redaction, document=document)


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LockDocumentCommand:
    """The input to ``lock_document.handle`` (P03-US8)."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID


@dataclass(frozen=True, slots=True)
class LockDocumentResult:
    """The outcome of a successful lock.

    ``document`` is the locked aggregate (``locked=True``,
    ``locked_at`` / ``locked_by`` populated). The HTTP layer returns
    the updated metadata to the caller.
    """

    document: Document


async def lock(
    cmd: LockDocumentCommand,
    *,
    document_repo: DocumentRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> LockDocumentResult:
    """Lock a document so it refuses further mutations.

    Steps:

    1. Authorize ``Action.DOCUMENT_LOCK`` on the document's tenant.
    2. Resolve the document within the actor's tenant; cross-tenant
       ids surface as ``NotFound``.
    3. ``Document.with_lock`` raises ``DocumentLocked`` if the
       document is already locked — the second attempt is a refused
       event, not a silent no-op.
    4. Persist the locked metadata via ``save_locked`` and append the
       audit + outbox rows in the same transaction.
    """
    await authorize(
        Action.DOCUMENT_LOCK,
        _UserContext(
            tenant_id=cmd.tenant_id,
            user_id=cmd.actor_id,
            roles=cmd.actor_roles,
        ),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
    )

    document: Document | None = await document_repo.get(cmd.tenant_id, cmd.document_id)
    if document is None:
        raise NotFound(f"document {cmd.document_id} not found")

    now = clock.now()
    locked = document.with_lock(actor_id=cmd.actor_id, now=now)

    await document_repo.save_locked(locked)

    correlation_id = _new_correlation_id()

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=DOCUMENT_LOCKED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DOCUMENT,
        subject_id=document.id,
        case_id=document.case_id,
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            "file_name": document.file_name,
            "classification": document.classification,
            "locked_at": now.isoformat(),
            "locked_by": str(cmd.actor_id),
            "version_token_locked": locked.current_version,
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=DOCUMENT_LOCKED,
        event_id=uuid4(),
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            "locked_at": now.isoformat(),
            "occurred_at": now.isoformat(),
        },
    )

    return LockDocumentResult(document=locked)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_hex(content: bytes) -> str:
    """SHA-256 of ``content`` as lowercase hex (canonical hashlib shape)."""
    return hashlib.sha256(content).hexdigest()


def _user_context(cmd: RedactDocumentCommand) -> Any:
    """Duck-typed ``UserContext`` for the redaction policy."""
    return _UserProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


def _UserContext(  # noqa: N802 — duck-typed, mirrors deps.UserContext
    *,
    tenant_id: UUID,
    user_id: UUID,
    roles: tuple[str, ...],
) -> Any:
    """Duck-typed ``UserContext`` for the lock policy.

    Defined locally so each use case is testable without dragging the
    deps module in (AGENTS.md §4.R10).
    """
    return _UserProxy(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=roles,
    )


def _new_correlation_id() -> str:
    """Return a fresh uuid7 string for the audit/outbox correlation id."""
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


__all__ = [
    "DOCUMENT_LOCKED",
    "LockDocumentCommand",
    "LockDocumentResult",
    "REDACTION_CREATED",
    "RedactDocumentCommand",
    "RedactDocumentResult",
    "lock",
    "redact",
]
