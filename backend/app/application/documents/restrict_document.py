"""Restrict a document's sensitivity label (FM-025 / T5).

Raising a document to ``restricted`` (or ``phi``) moves all future
reads under the strictly stronger ``document.read_restricted`` right.
The operation is the filing-safety primitive that lets an authorized
user quarantine a document whose bytes turned out to be sensitive
*after* upload, without rewriting history: the ``Document`` aggregate
keeps its identity, versions, and redactions; only the label flips.

Authorization: ``Action.DOCUMENT_REDACT`` (supervisor / process_admin).
The redact privilege is reused deliberately — restriction and
redaction are the same trust tier (both hide bytes from view-only
readers), so a separate action would double the role matrix for no
additional safety. The audit row records the before/after labels so
the timeline proves what changed.

Failure semantics: auth / not-found / lock refusal raise before any
DB write. The caller commits once so the label flip + audit + outbox
land atomically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.application.documents.upload_document import SUBJECT_TYPE_DOCUMENT
from app.domain.context import UserContext
from app.domain.document import Document, DocumentSensitivity
from app.domain.document_protection import check_mutation_allowed
from app.domain.errors import NotFound, ValidationFailed
from app.domain.ports import AuditEventStore, DocumentRepository, OutboxWriter

DOCUMENT_RESTRICTED = "DocumentRestricted"

#: Labels the restrict endpoint accepts. Only the two levels at or
#: above the restriction threshold are meaningful — lowering a label
#: is a separate declassification flow (deferred; fail-closed here).
RESTRICT_TARGETS: frozenset[DocumentSensitivity] = frozenset(
    {DocumentSensitivity.RESTRICTED, DocumentSensitivity.PHI}
)


@dataclass(frozen=True, slots=True)
class RestrictDocumentCommand:
    """Input to :func:`restrict`."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID
    sensitivity: DocumentSensitivity


@dataclass(frozen=True, slots=True)
class RestrictDocumentResult:
    """Outcome of a successful restriction."""

    document: Document
    previous_sensitivity: DocumentSensitivity


async def restrict(
    cmd: RestrictDocumentCommand,
    *,
    document_repo: DocumentRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> RestrictDocumentResult:
    """Raise a document's sensitivity to ``restricted`` / ``phi``.

    Steps: authorize ``DOCUMENT_REDACT`` → resolve within tenant
    (cross-tenant → ``NotFound``) → filing-lock gate → target-label
    validation → ``Document.with_sensitivity`` → ``save_sensitivity``
    → audit + outbox in the same transaction.
    """
    await authorize(
        Action.DOCUMENT_REDACT,
        UserContext(
            tenant_id=cmd.tenant_id,
            user_id=cmd.actor_id,
            display_name="",
            roles=cmd.actor_roles,
        ),
        Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
    )

    document = await document_repo.get(cmd.tenant_id, cmd.document_id)
    if document is None:
        raise NotFound(f"document {cmd.document_id} not found")

    # Why lock first: a filed record cannot be silently reclassified;
    # the refusal must hold regardless of target validity.
    check_mutation_allowed(document)

    if cmd.sensitivity not in RESTRICT_TARGETS:
        raise ValidationFailed(
            "restrict target must be 'restricted' or 'phi'",
            field="sensitivity",
        )

    previous = document.sensitivity
    now = clock.now()
    updated = document.with_sensitivity(sensitivity=cmd.sensitivity, now=now)
    await document_repo.save_sensitivity(updated)

    correlation_id = str(uuid4())

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=DOCUMENT_RESTRICTED,
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
            "previous_sensitivity": previous.value,
            "sensitivity": updated.sensitivity.value,
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=DOCUMENT_RESTRICTED,
        event_id=uuid4(),
        payload={
            "case_id": str(document.case_id),
            "document_id": str(document.id),
            "sensitivity": updated.sensitivity.value,
            "occurred_at": now.isoformat(),
        },
    )

    return RestrictDocumentResult(document=updated, previous_sensitivity=previous)


__all__ = [
    "DOCUMENT_RESTRICTED",
    "RESTRICT_TARGETS",
    "RestrictDocumentCommand",
    "RestrictDocumentResult",
    "restrict",
]
