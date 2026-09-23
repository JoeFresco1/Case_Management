"""Document upload use case (P03-US1, US4 / FM-022 / FR-005).

The canonical "attach a document to a case" flow:

1. **Authorize** the actor against ``Action.DOCUMENT_UPLOAD`` on the
   case's tenant (P11-US2, FM-002).
2. **Resolve the case** within the actor's tenant. A cross-tenant case
   is surfaced as ``Forbidden`` so we never leak the existence of a
   case in another tenant.
3. **Resolve the case-type** to learn the upload policy (allowed
   classifications, allowed content types, max size, P03-US4).
4. **Validate** the upload against the policy:
   - ``content_type`` must be in the case type's
     ``document_allowed_content_types`` (default: any).
   - ``size`` must be <= the case type's ``document_max_size_bytes``
     (default: 25 MiB).
   - ``classification`` must be one of the case type's
     ``document_classifications`` (P05-US5).
5. **Hash** the content with SHA-256 so identical bytes land at the
   same storage path and the metadata row records the digest
   (FR-005 acceptance scenario 1).
6. **Store bytes** through ``DocumentStore.put``. A storage failure
   raises ``DependencyUnavailable`` and rolls back; a successful write
   returns the ``storage_ref``.
7. **Persist** the ``Document`` metadata row, the initial
   ``DocumentVersion`` row, the ``DocumentAdded`` audit event, and the
   ``DocumentAdded`` outbox event in the same transaction. On
   rejection (policy fail), append a ``DocumentRejected`` audit event
   instead — no metadata row, no version row, no outbox row.

# INTEGRATION_SEAM: DocumentStore

The application calls ``DocumentStore.put`` from ``app.domain.ports``.
The local adapter (``LocalDocumentStore``) is the only implementation
today; the Azure Blob adapter is the production seam
(``docs/integrations/seams.md``).

# Configuration

A ``CaseTypeVersion.schema`` carries the policy:

- ``schema.document_max_size_bytes`` (int) — the default 25 MiB
  ceiling when omitted.
- ``schema.document_allowed_content_types`` (tuple[str, ...]) — when
  empty, every content type is allowed (synthetic-data convenience).
- ``schema.document_classifications`` (tuple[str, ...]) — the allowed
  classification labels; a label not in the list is rejected.
- ``schema.document_reject_duplicates`` (bool, default False) — when
  true, an upload whose SHA-256 already exists on the same case is
  rejected with ``ValidationFailed`` before bytes are stored (T055).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from app.application.authorization import Action, Resource, authorize
from app.domain.case import Case
from app.domain.case_type import CaseTypeVersion
from app.domain.document import (
    Document,
    DocumentSensitivity,
    DocumentVersion,
)
from app.domain.errors import (
    NotFound,
    ValidationFailed,
)
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    DocumentRepository,
    DocumentStore,
    OutboxWriter,
)

# Stable string constants for the audit + outbox payloads. Subscribers
# depend on these literals.
SUBJECT_TYPE_DOCUMENT = "document"

# Stable event types. ``DocumentAdded`` is in the canonical
# ``app.domain.audit`` catalog; ``DocumentRejected`` is a first-slice
# extension used by the use case on policy failure. Adding a new event
# type to ``app.domain.audit.ALL_EVENT_TYPES` is a separate concern
# (P09 §1) — for now we record the rejection with the same audit
# machinery but a distinct event_type literal.
DOCUMENT_ADDED = "DocumentAdded"
DOCUMENT_REJECTED = "DocumentRejected"


# Default upload ceiling. The case-type policy overrides this when
# present. 25 MiB matches the synthetic seed and matches the
# P03 pillar's "default maximum" intent.
DEFAULT_MAX_SIZE_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class UploadDocumentCommand:
    """The input to ``upload_document.handle``.

    Fields mirror what the HTTP layer receives as multipart fields
    plus the binary payload.
    """

    tenant_id: UUID
    actor_id: UUID
    actor_roles: tuple[str, ...]
    case_id: UUID
    file_name: str
    content_type: str
    classification: str
    sensitivity: DocumentSensitivity
    content: bytes


@dataclass(frozen=True, slots=True)
class UploadDocumentResult:
    """The outcome of a successful upload.

    ``document`` and ``version`` are returned so the HTTP layer can
    build the wire response and the use case can be tested without
    touching the DB.
    """

    document: Document
    version: DocumentVersion


async def handle(
    cmd: UploadDocumentCommand,
    *,
    case_repo: CaseRepository,
    case_type_version_repo: Any,
    document_repo: DocumentRepository,
    document_store: DocumentStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    clock: Any,
) -> UploadDocumentResult:
    """Upload a document atomically with audit + outbox.

    Failure behavior:
    - Validation/auth/not-found errors are raised before any DB write.
    - On a rejected upload, a ``DocumentRejected`` audit event is
      appended (in the same transaction, so the rejection is durable)
      and no metadata or version row is written.
    - On storage failure (``put`` raising ``DependencyUnavailable``),
      the caller is expected to roll the transaction back; no audit
      row is appended in that case because there is no documented
      domain change. An optional follow-up audit row could be added by
      the HTTP layer if operator visibility is required.
    """
    # 1. Authorization. The policy raises `Forbidden` on deny.
    await authorize(
        Action.DOCUMENT_UPLOAD,
        _user_context(cmd),
        Resource(tenant_id=cmd.tenant_id, kind="case", id=cmd.case_id),
    )

    # 2. Resolve the case within the actor's tenant.
    case: Case | None = await case_repo.get(cmd.tenant_id, cmd.case_id)
    if case is None:
        raise NotFound(f"case {cmd.case_id} not found")

    # 3. Resolve the case-type version to read the upload policy.
    version: CaseTypeVersion | None = await case_type_version_repo.get(
        cmd.tenant_id, case.case_type_version_id
    )
    if version is None:
        # The case row references a version that has since been deleted;
        # the upload cannot proceed because we cannot validate the
        # policy. Surface as NotFound so the HTTP layer returns 404.
        raise NotFound(f"case type version {case.case_type_version_id} not found")

    # 4. Policy validation. A failure here records ``DocumentRejected``
    # audit and raises ``ValidationFailed``.
    await _validate_against_policy(cmd, version)

    # 5. Hash content. Hashing is the caller's responsibility — the
    # use case is the boundary that decides what "the digest" means
    # (lowercase hex, 64 chars).
    sha256 = _sha256_hex(cmd.content)
    size = len(cmd.content)

    # 5a. Duplicate rejection (T055). When the case type opts in via
    # ``document_reject_duplicates``, an upload whose digest already
    # exists on the same case is refused *before* storing bytes — no
    # orphaned content, no metadata row. The router records the
    # ``DocumentRejected`` audit from the ``ValidationFailed`` below,
    # exactly like a policy rejection.
    if version.schema.document_reject_duplicates:
        for existing in await document_repo.list_for_case(tenant_id=cmd.tenant_id, case_id=case.id):
            if existing.sha256 == sha256:
                raise ValidationFailed(
                    "duplicate document: identical content already exists on this case",
                    field="content",
                )

    # 6. Store bytes. A storage failure is a ``DependencyUnavailable``
    # and is **not** audited as DocumentRejected because there is no
    # documented domain change; the use case re-raises for the HTTP
    # layer to translate to 503 / dependency_unavailable.
    storage_ref = await document_store.put(
        tenant_id=cmd.tenant_id,
        content=cmd.content,
        content_type=cmd.content_type,
        sha256=sha256,
    )

    # 7. Persist metadata + version + audit + outbox in the same
    # transaction.
    now = clock.now()
    document = Document.create(
        tenant_id=cmd.tenant_id,
        case_id=case.id,
        file_name=cmd.file_name,
        content_type=cmd.content_type,
        classification=cmd.classification,
        sensitivity=cmd.sensitivity,
        sha256=sha256,
        size=size,
        storage_ref=storage_ref,
        uploaded_by=cmd.actor_id,
        now=now,
    )
    version_row = DocumentVersion(
        id=uuid4(),
        tenant_id=cmd.tenant_id,
        document_id=document.id,
        version_number=1,
        sha256=sha256,
        size=size,
        storage_ref=storage_ref,
        content_type=cmd.content_type,
        uploaded_by=cmd.actor_id,
        uploaded_at=now,
    )

    correlation_id = _new_correlation_id()

    await document_repo.add_document_and_version(document=document, version=version_row)

    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=DOCUMENT_ADDED,
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
            "content_type": document.content_type,
            "classification": document.classification,
            "sensitivity": document.sensitivity.value,
            "sha256": document.sha256,
            "size": document.size,
            "version_number": version_row.version_number,
            "storage_ref": document.storage_ref,
        },
        occurred_at=now,
    )

    await outbox.enqueue(
        tenant_id=cmd.tenant_id,
        event_type=DOCUMENT_ADDED,
        event_id=uuid4(),
        payload={
            "case_id": str(case.id),
            "document_id": str(document.id),
            "version_id": str(version_row.id),
            "sha256": sha256,
            "size": size,
            "content_type": cmd.content_type,
            "classification": cmd.classification,
            "occurred_at": now.isoformat(),
        },
    )

    return UploadDocumentResult(document=document, version=version_row)


async def record_rejection(
    cmd: UploadDocumentCommand,
    *,
    audit: AuditEventStore,
    clock: Any,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Append a ``DocumentRejected`` audit row for a refused upload.

    Called by the HTTP layer when the policy rejects an upload before
    any DB write. The audit row is the only persisted artifact — no
    document or version row exists, so there is no outbox row either
    (subscribers do not act on rejected uploads).
    """
    payload: dict[str, Any] = {
        "case_id": str(cmd.case_id),
        "file_name": cmd.file_name,
        "content_type": cmd.content_type,
        "classification": cmd.classification,
        "size": len(cmd.content),
        "reason": reason,
    }
    if details:
        payload["details"] = details
    now = clock.now()
    await audit.append(
        tenant_id=cmd.tenant_id,
        event_type=DOCUMENT_REJECTED,
        actor_id=cmd.actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_DOCUMENT,
        subject_id=cmd.case_id,  # the subject is the target case (no document yet)
        case_id=cmd.case_id,
        correlation_id=_new_correlation_id(),
        causation_id=None,
        payload=payload,
        occurred_at=now,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _validate_against_policy(cmd: UploadDocumentCommand, version: CaseTypeVersion) -> None:
    """Apply the case-type upload policy to ``cmd``.

    Raises ``ValidationFailed`` on the first policy violation. The
    caller (HTTP layer) is expected to translate the failure to a
    ``DocumentRejected`` audit via ``record_rejection``.
    """
    policy = _policy_from(version)
    allowed_types = policy["allowed_content_types"]
    if allowed_types and cmd.content_type not in allowed_types:
        raise ValidationFailed(
            f"content type {cmd.content_type!r} is not permitted for this case type",
            field="content_type",
        )
    max_size = policy["max_size_bytes"]
    if max_size and len(cmd.content) > max_size:
        raise ValidationFailed(
            f"document exceeds configured max size ({max_size} bytes)",
            field="size",
        )
    allowed_classifications = policy["allowed_classifications"]
    if allowed_classifications and cmd.classification not in allowed_classifications:
        raise ValidationFailed(
            f"classification {cmd.classification!r} is not permitted for this case type",
            field="classification",
        )


def _policy_from(version: CaseTypeVersion) -> dict[str, Any]:
    """Read the upload policy out of the case-type schema.

    The schema is a JSON shape (AGENTS.md §16.2). The policy keys are
    optional so older case types without explicit policy continue to
    work; defaults applied here are the synthetic-friendly values
    that match the seed dataset.
    """
    schema = version.schema
    allowed_types = getattr(schema, "document_allowed_content_types", ()) or ()
    max_size = getattr(schema, "document_max_size_bytes", 0) or 0
    if not max_size:
        max_size = DEFAULT_MAX_SIZE_BYTES
    return {
        "allowed_content_types": tuple(allowed_types),
        "max_size_bytes": max_size,
        "allowed_classifications": tuple(schema.document_classifications),
    }


def _sha256_hex(content: bytes) -> str:
    """SHA-256 of ``content`` as lowercase hex (canonical hashlib shape)."""
    return hashlib.sha256(content).hexdigest()


def _user_context(cmd: UploadDocumentCommand) -> Any:
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

    Mirrors the proxy used in ``app.application.cases.create_case``;
    kept local so each use case is testable without dragging the deps
    module in.
    """

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _new_correlation_id() -> str:
    """Return a fresh uuid7 string for the audit/outbox correlation id.

    PyPI name ``uuid7`` ships its module as ``uuid_extensions``
    (uuid7 was a placeholder during early publishing). Lazy import
    keeps cold-start light and survives a missing package (test seam).
    """
    from uuid_extensions import uuid7  # noqa: PLC0415

    return str(uuid7())


# ---------------------------------------------------------------------------
# No persistence hook seam
# ---------------------------------------------------------------------------
#
# The use case takes a ``DocumentRepository`` directly. Production
# wiring is done by the HTTP layer; tests inject an in-memory recorder.


__all__ = [
    "DEFAULT_MAX_SIZE_BYTES",
    "DOCUMENT_ADDED",
    "DOCUMENT_REJECTED",
    "SUBJECT_TYPE_DOCUMENT",
    "UploadDocumentCommand",
    "UploadDocumentResult",
    "handle",
    "record_rejection",
]
