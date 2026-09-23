"""Download case document bytes through the storage port (P03-US2 / FR-005 / FM-025).

The use case keeps document metadata and physical bytes separate. It resolves
the document within the actor's tenant, authorizes the read, retrieves bytes
from ``DocumentStore``, and appends a metadata-only access audit event.

FM-025 hardening (T5):

- **Restriction gate.** A document whose sensitivity is ``restricted`` /
  ``phi`` requires the strictly stronger ``document.read_restricted``
  grant. A guessed-URL download without that grant raises ``Forbidden``
  with a safe ``reason`` code only — no file name, storage ref, size,
  or content in the error details.
- **Renditions.** ``rendition="original"`` serves the pre-redaction
  bytes; ``rendition="redacted"`` serves the ACTIVE redaction's bytes.
  The original always requires the stricter right once a redaction
  exists (even when the parent itself is merely ``internal`` — the
  original still holds exactly the bytes a redaction removed).
  Each rendition download appends its own ``DocumentAccessed`` audit
  row carrying the ``rendition`` + ``redaction_id`` so both views are
  independently auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from app.application.authorization import Action, Resource, authorize
from app.domain.context import UserContext
from app.domain.document import Document, DocumentRedaction
from app.domain.document_protection import check_read_allowed, is_access_restricted
from app.domain.errors import Forbidden, NotFound, ValidationFailed
from app.domain.ports import AuditEventStore, DocumentRepository, DocumentStore

DOCUMENT_ACCESSED = "DocumentAccessed"

Rendition = Literal["original", "redacted"]


@dataclass(frozen=True, slots=True)
class DownloadDocumentCommand:
    """Inputs for a tenant-scoped document download."""

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    document_id: UUID
    correlation_id: str = ""
    # Why default "original": the pre-rendition API served the stored
    # bytes; keeping the default preserves backward compatibility for
    # existing callers while the redacted view is opt-in.
    rendition: Rendition = "original"


@dataclass(frozen=True, slots=True)
class DownloadDocumentResult:
    """Document metadata and bytes returned to the HTTP boundary."""

    document: Document
    content: bytes
    rendition: Rendition = "original"
    redaction: DocumentRedaction | None = None


def _granted_permissions(actor_roles: tuple[str, ...]) -> frozenset[str]:
    """Map role names to the live permission strings for the read gate.

    Why not call the full policy twice: ``authorize`` already enforces
    the base ``document.read`` grant. The domain
    ``check_read_allowed`` needs the *permission* union to decide
    whether the stricter right is present. The mapping below mirrors
    the static role registry (supervisor / process_admin hold the
    stricter right); live DB role grants are enforced by the second
    ``authorize(DOCUMENT_READ_RESTRICTED)`` call in :func:`handle`
    when the stricter right is required, so a custom role granted
    ``document.read_restricted`` via FM-048 is honoured even though it
    is absent from this static set.
    """
    granted: set[str] = set()
    for role in actor_roles:
        if role in ("supervisor", "process_admin"):
            granted.add("document.read_restricted")
        if role in ("reviewer", "supervisor", "process_admin"):
            granted.add("document.read")
    return frozenset(granted)


async def handle(
    cmd: DownloadDocumentCommand,
    *,
    document_repo: DocumentRepository,
    document_store: DocumentStore,
    audit: AuditEventStore,
    clock: Any,
) -> DownloadDocumentResult:
    """Authorize and retrieve a document rendition, recording access.

    Raises ``NotFound`` for a missing or cross-tenant document (the two
    are indistinguishable) and ``Forbidden`` with a metadata-free
    ``reason`` when the actor lacks the required grant. Storage
    failures propagate as ``DependencyUnavailable`` without exposing
    document bytes. The caller owns the transaction commit; the audit
    row is staged only after successful retrieval.
    """
    await authorize(
        Action.DOCUMENT_READ,
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

    if cmd.rendition not in ("original", "redacted"):
        raise ValidationFailed(
            "rendition must be 'original' or 'redacted'",
            field="rendition",
        )

    active_redaction: DocumentRedaction | None = None
    get_active = getattr(document_repo, "get_active_redaction", None)
    if get_active is not None:
        active_redaction = await get_active(cmd.tenant_id, cmd.document_id)

    if cmd.rendition == "redacted":
        if active_redaction is None:
            raise NotFound(f"no active redaction for document {cmd.document_id}")
        # Why base gate on the parent label: the redacted view hides
        # the removed bytes, so it follows the parent's own threshold
        # (restricted parent → stricter right; internal parent → base).
        check_read_allowed(
            document=document,
            actor_tenant_id=cmd.tenant_id,
            granted_permissions=_granted_permissions(cmd.actor_roles),
            for_original=False,
        )
        if is_access_restricted(document):
            # Why a second authorize: the static permission map above
            # could drift from FM-048 live grants; the policy is the
            # authority for the stricter right and its denial carries
            # the standard role-reason (still metadata-free).
            try:
                await authorize(
                    Action.DOCUMENT_READ_RESTRICTED,
                    UserContext(
                        tenant_id=cmd.tenant_id,
                        user_id=cmd.actor_id,
                        display_name="",
                        roles=cmd.actor_roles,
                    ),
                    Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
                )
            except Forbidden as exc:
                # Why re-raise with a safe reason: the policy denial
                # message names the action; the guessed-URL contract
                # requires only the stable reason code.
                raise Forbidden(
                    "document read denied",
                    details={"reason": "restricted_document"},
                ) from exc
        content = await document_store.get(
            tenant_id=cmd.tenant_id,
            content_ref=active_redaction.storage_ref,
        )
        await audit.append(
            tenant_id=cmd.tenant_id,
            event_type=DOCUMENT_ACCESSED,
            actor_id=cmd.actor_id,
            actor_type="user",
            subject_type="document",
            subject_id=document.id,
            case_id=document.case_id,
            correlation_id=cmd.correlation_id,
            causation_id=None,
            payload={
                "document_id": str(document.id),
                "version": document.current_version,
                "rendition": "redacted",
                "redaction_id": str(active_redaction.id),
                "base_version_number": active_redaction.base_version_number,
            },
            occurred_at=clock.now(),
        )
        return DownloadDocumentResult(
            document=document, content=content, rendition="redacted", redaction=active_redaction
        )

    # rendition == "original": the original always requires the
    # stricter right once a redaction exists (it holds the removed
    # bytes) or when the parent itself is access-restricted.
    restricted = is_access_restricted(document)
    has_redaction = active_redaction is not None
    needs_stricter = restricted or has_redaction
    check_read_allowed(
        document=document,
        actor_tenant_id=cmd.tenant_id,
        granted_permissions=_granted_permissions(cmd.actor_roles),
        # Why not always for_original: the guessed-URL contract needs
        # distinct reasons — a restricted parent reports
        # "restricted_document" while an internal parent with a
        # redaction reports "original_requires_stricter_right".
        for_original=(has_redaction and not restricted),
    )
    if needs_stricter:
        try:
            await authorize(
                Action.DOCUMENT_READ_RESTRICTED,
                UserContext(
                    tenant_id=cmd.tenant_id,
                    user_id=cmd.actor_id,
                    display_name="",
                    roles=cmd.actor_roles,
                ),
                Resource(tenant_id=cmd.tenant_id, kind="document", id=cmd.document_id),
            )
        except Forbidden as exc:
            reason = "restricted_document" if restricted else "original_requires_stricter_right"
            raise Forbidden(
                "document read denied",
                details={"reason": reason},
            ) from exc

    content = await document_store.get(
        tenant_id=cmd.tenant_id,
        content_ref=document.storage_ref,
    )

    # why: access audit payloads must identify the object without copying
    # protected content, filenames, or storage paths into durable audit data.
    # The rendition marker lets the timeline distinguish "saw the
    # redacted view" from "saw the original bytes" for the same document.
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=DOCUMENT_ACCESSED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type="document",
        subject_id=document.id,
        case_id=document.case_id,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "document_id": str(document.id),
            "version": document.current_version,
            "rendition": "original",
        },
        occurred_at=clock.now(),
    )
    return DownloadDocumentResult(document=document, content=content, rendition="original")


__all__ = ["DOCUMENT_ACCESSED", "DownloadDocumentCommand", "DownloadDocumentResult", "handle"]
