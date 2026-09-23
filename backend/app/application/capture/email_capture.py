"""Inbound email capture use case (P04-US6 / FM-028).

The use case turns an approved inbound email into a first-class
domain object with channel + message provenance, and dedups by the
RFC 5322 ``Message-Id`` so a re-delivered message is silently a
no-op on the artifact side and the prior result is returned.

# Flow

1. **Authorize** the actor against ``Action.DOCUMENT_UPLOAD`` when the
   email is destined for a case's document store (P11-US2). The
   authorization decision is delegated to the same policy the upload
   path uses so a caller who can upload a document can also file an
   inbound email against that case.
2. **Resolve the channel** for the actor's tenant. The channel must
   be of kind ``ChannelKind.EMAIL`` and **enabled** — a disabled
   channel refuses new captures (fail-closed, mirrors
   ``quarantine_capture``).
3. **Dedup by Message-Id**: ``CapturedEmailRepository.get_by_message_id``
   is consulted first. A hit returns the prior ``CapturedEmail``
   record unchanged — no document written, no audit row, no outbox
   row, no storage call. This is the content idempotency layer; it
   composes with the HTTP ``Idempotency-Key`` middleware but is
   independent of it.
4. **Hash + store** the email body through ``DocumentStore``. The
   ``Document`` aggregate's content hash and size are recorded.
5. **Persist** ``Document`` + ``DocumentVersion`` + ``CapturedEmail`` +
   ``EmailCaptured`` audit + outbox in the **same transaction**. A
   partial commit would leave an artifact without provenance or a
   provenance row without an artifact — both are explicitly forbidden.

# Failure behavior

- Validation (empty body / missing Message-Id / unknown case /
  disabled channel / authorization denial) raises before any DB
  write.
- A storage ``DependencyUnavailable`` propagates upward; the caller
  rolls back so no partial state lands.
- A duplicate ``Message-Id`` returns the prior result and writes
  nothing; the audit + outbox trail is not duplicated.

# INTEGRATION_SEAM: DocumentStore + email source

The local ``DocumentStore`` is the only storage adapter today; the
Graph / IMAP connector (P14) feeds this same use case. The seam is
the ``CaptureChannel`` port + the ``EmailSource`` port (future); the
first slice accepts JSON-shaped emails over HTTP so the operator UI
can replay synthetic mailboxes without a live SMTP / Graph path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.channel import ChannelKind
from app.domain.document import Document, DocumentSensitivity, DocumentVersion
from app.domain.email_capture import CapturedEmail, is_valid_message_id
from app.domain.errors import NotFound, ValidationFailed
from app.domain.ports import (
    AuditEventStore,
    CapturedEmailRepository,
    CaseRepository,
    ChannelRepository,
    DocumentRepository,
    DocumentStore,
    OutboxWriter,
)

# Stable subject-type + event-type literals. Subscribers (audit timeline,
# outbox consumers) depend on these strings.
SUBJECT_TYPE_EMAIL = "email_capture"
SUBJECT_TYPE_DOCUMENT = "document"

EVENT_EMAIL_CAPTURED = "EmailCaptured"

# Reasonable email body ceiling — mirrors the default document size
# cap used by ``upload_document``. The case-type policy can override
# at the upload layer; for email capture we apply this default
# because the channel is the validation seam, not the case type.
DEFAULT_MAX_EMAIL_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CaptureEmailCommand:
    """The input to ``capture_email``.

    ``tenant_id`` is taken from the authenticated user, not the
    request body. ``case_id`` is optional: when omitted the email is
    filed as a free-standing document; when supplied it must resolve
    to a case within the actor's tenant.

    ``body`` is the raw RFC 5322 message bytes (or the JSON-rendered
    body in this first slice). The platform stores the bytes — never
    parses them — so a downstream subsystem can choose its own parser.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    channel_id: UUID
    message_id: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    body: bytes
    case_id: UUID | None = None
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class CaptureEmailResult:
    """The outcome of a successful (or idempotently-replayed) capture.

    ``replayed=True`` means a prior ``CapturedEmail`` was found for
    the same Message-Id; the artifact row in this response is the
    *original* one and no new document was written. ``document`` is
    ``None`` only when ``destination="case_note"`` — the document
    destination always populates it (case-note path is deferred).
    """

    captured: CapturedEmail
    document: Document | None
    document_version: DocumentVersion | None
    replayed: bool


async def capture_email(
    cmd: CaptureEmailCommand,
    *,
    channel_repo: ChannelRepository,
    case_repo: CaseRepository,
    document_repo: DocumentRepository,
    document_store: DocumentStore,
    captured_email_repo: CapturedEmailRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> CaptureEmailResult:
    """Capture one inbound email atomically with audit + outbox.

    Failure behavior:
    - Auth / validation / not-found errors are raised before any DB
      write.
    - On a duplicate ``Message-Id`` the prior ``CapturedEmail`` is
      returned without writing a new document, audit row, or outbox
      row — the ``replayed`` flag on the result distinguishes the
      idempotent replay from a fresh capture.
    - On a storage failure the use case propagates ``DependencyUnavailable``;
      no metadata / version / captured-email / audit / outbox row is
      written. The caller's transaction rollback keeps partial state
      out of the database.
    """
    # 1. Pre-flight validation. All checks here are cheap and raise
    # before any I/O so a malformed capture never touches storage.
    if not is_valid_message_id(cmd.message_id):
        raise ValidationFailed(
            "message_id is required and must be a non-empty string",
            field="message_id",
        )
    if not cmd.body:
        raise ValidationFailed(
            "email body is required",
            field="body",
        )
    if len(cmd.body) > DEFAULT_MAX_EMAIL_BYTES:
        raise ValidationFailed(
            f"email body exceeds configured max size ({DEFAULT_MAX_EMAIL_BYTES} bytes)",
            field="body",
        )

    # 2. Resolve the channel. The channel must be of kind EMAIL and
    # enabled — a disabled channel is a fail-closed refusal (mirrors
    # ``quarantine_capture`` in ``manage_channels``).
    channel = await channel_repo.get(cmd.tenant_id, cmd.channel_id)
    if channel is None:
        raise NotFound(f"channel {cmd.channel_id} not found in tenant")
    if channel.kind is not ChannelKind.EMAIL:
        raise ValidationFailed(
            f"channel {cmd.channel_id} is not an email channel (kind={channel.kind.value})",
            field="channel_id",
        )
    if not channel.enabled:
        raise ValidationFailed(
            f"channel {channel.key!r} is disabled",
            field="channel_id",
        )

    # 3. Content idempotency. A prior capture with the same
    # Message-Id wins — return it without re-running the capture
    # pipeline. The HTTP ``Idempotency-Key`` middleware is a separate
    # layer (replay by header); both compose.
    prior = await captured_email_repo.get_by_message_id(
        tenant_id=cmd.tenant_id,
        channel_id=channel.id,
        message_id=cmd.message_id.strip(),
    )
    if prior is not None:
        return CaptureEmailResult(
            captured=prior,
            document=None,
            document_version=None,
            replayed=True,
        )

    # 4. If a case_id was supplied, resolve it within the actor's
    # tenant. We require the actor to be allowed to upload a document
    # to that case (the email becomes a document on that case, so
    # the same gate that guards ``POST /cases/{id}/documents`` guards
    # this flow). A cross-tenant or unknown case surfaces as
    # ``NotFound`` so the API never confirms cross-tenant existence
    # (Constitution V).
    case = None
    if cmd.case_id is not None:
        case = await case_repo.get(cmd.tenant_id, cmd.case_id)
        if case is None:
            raise NotFound(f"case {cmd.case_id} not found in tenant")
        await authorize(
            Action.DOCUMENT_UPLOAD,
            _user_context(cmd),
            Resource(tenant_id=cmd.tenant_id, kind="case", id=case.id),
        )

    # 5. Hash + store the email bytes. The DocumentStore is the
    # canonical seam; the application layer is responsible only for
    # computing the digest and the size. A ``DependencyUnavailable``
    # propagates; no DB row is written yet.
    sha256 = hashlib.sha256(cmd.body).hexdigest()
    size = len(cmd.body)
    content_type = "message/rfc822"
    file_name = _email_file_name(cmd.subject, cmd.message_id)
    storage_ref = await document_store.put(
        tenant_id=cmd.tenant_id,
        content=cmd.body,
        content_type=content_type,
        sha256=sha256,
    )

    # 6. Persist (Document + DocumentVersion + CapturedEmail +
    # audit + outbox) in the SAME transaction.
    now = clock.now()
    message_id = cmd.message_id.strip()

    # A captured email without a case is a free-standing document —
    # the case_id column on the documents table is NOT NULL per the
    # P03 schema, so we require a case for the document destination.
    # Case-note destination is deferred; in this slice we route every
    # approved email to a case document, returning ``NotFound`` when
    # no case is supplied (the operator UI always supplies one).
    if case is None:
        # Free-standing documents are out of scope for this slice
        # (documents.case_id is NOT NULL). The HTTP layer surfaces
        # this as 422 via the ValidationFailed -> ErrorResponse
        # mapping in ``app/interfaces/http/errors.py``.
        raise ValidationFailed(
            "case_id is required for email capture in this slice",
            field="case_id",
        )

    document = Document.create(
        tenant_id=cmd.tenant_id,
        case_id=case.id,
        file_name=file_name,
        content_type=content_type,
        classification="Correspondence",
        sensitivity=DocumentSensitivity.INTERNAL,
        sha256=sha256,
        size=size,
        storage_ref=storage_ref,
        uploaded_by=cmd.actor_id,
        now=now,
    )
    document_version = DocumentVersion(
        id=uuid4(),
        tenant_id=cmd.tenant_id,
        document_id=document.id,
        version_number=1,
        sha256=sha256,
        size=size,
        storage_ref=storage_ref,
        content_type=content_type,
        uploaded_by=cmd.actor_id,
        uploaded_at=now,
    )
    await document_repo.add_document_and_version(document=document, version=document_version)

    captured = CapturedEmail(
        id=uuid4(),
        tenant_id=cmd.tenant_id,
        channel_id=channel.id,
        channel_key=channel.key,
        message_id=message_id,
        sender=cmd.sender,
        recipients=tuple(cmd.recipients),
        subject=cmd.subject,
        captured_at=now,
        captured_by=cmd.actor_id,
        destination="document",
        artifact_id=document.id,
        artifact_kind=SUBJECT_TYPE_DOCUMENT,
        correlation_id=cmd.correlation_id,
        case_id=case.id,
    )
    await captured_email_repo.add(captured)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_EMAIL_CAPTURED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_EMAIL,
        subject_id=captured.id,
        case_id=case.id,
        correlation_id=cmd.correlation_id,
        causation_id=None,
        payload={
            "channel_id": str(channel.id),
            "channel_key": channel.key,
            "message_id": message_id,
            "sender": cmd.sender,
            "subject": cmd.subject,
            "recipients": list(cmd.recipients),
            "destination": captured.destination,
            "artifact_id": str(document.id),
            "artifact_kind": captured.artifact_kind,
            "sha256": sha256,
            "size": size,
            "storage_ref": storage_ref,
            "case_id": str(case.id),
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=EVENT_EMAIL_CAPTURED,
        event_id=uuid4(),
        payload={
            "captured_email_id": str(captured.id),
            "channel_id": str(channel.id),
            "channel_key": channel.key,
            "tenant_id": str(cmd.tenant_id),
            "message_id": message_id,
            "sender": cmd.sender,
            "subject": cmd.subject,
            "destination": captured.destination,
            "artifact_id": str(document.id),
            "case_id": str(case.id),
            "occurred_at": now.isoformat(),
        },
    )

    return CaptureEmailResult(
        captured=captured,
        document=document,
        document_version=document_version,
        replayed=False,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _email_file_name(subject: str, message_id: str) -> str:
    """Build a stable, safe file name for the captured email document.

    The subject is sanitized to a portable form (no slashes, capped
    length); the Message-Id is appended so the storage_ref stays
    unique even when two emails share a subject. RFC 5322 Message-Ids
    may carry angle brackets which are stripped here.
    """
    sanitized_subject = _sanitize_for_filename(subject) or "email"
    safe_message_id = _sanitize_for_filename(message_id.strip().strip("<>"))
    return f"{sanitized_subject}__msg-{safe_message_id}.eml"


def _sanitize_for_filename(value: str) -> str:
    """Strip / replace characters that break portable file names.

    Keeps alnum, dash, underscore, dot. Truncates to 200 chars.
    """
    out: list[str] = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:200]


def _user_context(cmd: CaptureEmailCommand) -> Any:
    """Duck-typed ``UserContext`` for the policy.

    The application layer does not import ``app.deps.UserContext`` to
    keep the boundary free of FastAPI. The policy reads only
    ``tenant_id``, ``roles``, and ``is_system``.
    """
    return _UserProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


@dataclass(frozen=True, slots=True)
class _UserProxy:
    """Duck-typed ``UserContext`` for the policy.

    Mirrors the proxy used in ``app.application.cases.create_case``
    and ``app.application.documents.upload_document`` so the policy
    can be exercised without dragging FastAPI into the test surface.
    """

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


__all__ = [
    "DEFAULT_MAX_EMAIL_BYTES",
    "EVENT_EMAIL_CAPTURED",
    "SUBJECT_TYPE_DOCUMENT",
    "SUBJECT_TYPE_EMAIL",
    "CaptureEmailCommand",
    "CaptureEmailResult",
    "capture_email",
]
