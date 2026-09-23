"""Commit-path persistence: create one case (and an optional document) per row (P14-US3)."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from app.application.imports.run_import.commands import ImportRow
from app.application.imports.run_import.helpers import (
    DOCUMENT_ADDED,
    SUBJECT_TYPE_CASE,
    SUBJECT_TYPE_DOCUMENT,
)
from app.application.imports.run_import.rows import _build_row
from app.domain.case import Case
from app.domain.case_type import CaseTypeVersion
from app.domain.document import (
    Document,
    DocumentSensitivity,
    DocumentVersion,
)
from app.domain.errors import ValidationFailed
from app.domain.import_job import MappingVersion
from app.domain.ports import (
    AuditEventStore,
    CaseRepository,
    DocumentRepository,
    DocumentStore,
    OutboxWriter,
)

# ---------------------------------------------------------------------------
# Commit path — create one case (and an optional document) per row
# ---------------------------------------------------------------------------


async def _create_case_for_row(
    *,
    row: ImportRow,
    mapping: MappingVersion,
    version: CaseTypeVersion,
    case_repo: CaseRepository,
    document_repo: DocumentRepository,
    document_store: DocumentStore,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    actor_id: UUID,
    tenant_id: UUID,
    correlation_id: str,
    now: Any,
) -> UUID:
    """Persist a single imported row and return the new case id.

    Per-record atomicity: document bytes are stored *before* any DB
    row is staged, so a storage :class:`DependencyUnavailable` leaves
    zero staged rows for this record (no partial case without its
    document). The caller quarantines that row and continues the
    batch; the outer ``db.commit()`` still commits the surviving
    records atomically with their audit + outbox rows.

    Raises:
        ValidationFailed: On field or document validation failure.
        NotFound / InvariantViolation: On config races.
        DependencyUnavailable: When ``DocumentStore.put`` is unavailable.
    """
    data, title, description, priority, document_payload = _build_row(row, mapping, version)

    # Store document bytes first — no DB write has happened yet for
    # this record, so a storage failure stages nothing. The decoded
    # bytes + ref travel with the record into the staging step below.
    # why: mirrors ``upload_document`` (store-then-stage); reversing
    # the order would strand a case row when the store is offline.
    pending_doc: tuple[bytes, str, str] | None = None
    if mapping.documents is not None and document_payload is not None:
        try:
            raw = base64.b64decode(document_payload["content_field"], validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValidationFailed(
                f"document content is not valid base64: {exc!s}",
                field="document",
            ) from exc
        sha = document_payload["sha256_field"]
        storage_ref = await document_store.put(
            tenant_id=tenant_id,
            content=raw,
            content_type=document_payload["content_type_field"],
            sha256=sha,
        )
        pending_doc = (raw, sha, storage_ref)

    # FM-009: the mapping names the case type, so imported cases honor
    # the tenant's `{TYPE}` reference template like a fresh intake.
    reference = await case_repo.next_reference(tenant_id, now=now, type_key=mapping.case_type_key)
    case = Case.create(
        tenant_id=tenant_id,
        reference=reference,
        case_type_version=version,
        title=title,
        description=description,
        data=data,
        priority=priority,
        created_by=actor_id,
        now=now,
        sensitivity=version.schema.document_max_size_bytes
        and "internal"
        or "internal",  # placeholder; case_type carries the default
    )
    await case_repo.add(case)

    await audit.append(
        tenant_id=tenant_id,
        event_type="CaseCreated",
        actor_id=actor_id,
        actor_type="user",
        subject_type=SUBJECT_TYPE_CASE,
        subject_id=case.id,
        case_id=case.id,
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            "reference": case.reference.value,
            "case_type_key": mapping.case_type_key,
            "case_type_version_id": str(case.case_type_version_id),
            "priority": case.priority,
            "title": case.title,
            "status": case.status.value,
            "source": "import",
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=tenant_id,
        event_type="CaseCreated",
        event_id=uuid4(),
        payload={
            "case_id": str(case.id),
            "reference": case.reference.value,
            "case_type_key": mapping.case_type_key,
            "tenant_id": str(tenant_id),
            "actor_id": str(actor_id),
            "occurred_at": now.isoformat(),
            "source": "import",
        },
    )

    # Stage the document metadata now that the case row exists. The
    # bytes were already stored above; this step only writes DB rows.
    if pending_doc is not None and document_payload is not None:
        await _stage_imported_document(
            case=case,
            mapping=mapping,
            document_payload=document_payload,
            raw=pending_doc[0],
            sha256=pending_doc[1],
            storage_ref=pending_doc[2],
            document_repo=document_repo,
            audit=audit,
            outbox=outbox,
            actor_id=actor_id,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            now=now,
        )

    return case.id


async def _stage_imported_document(
    *,
    case: Case,
    mapping: MappingVersion,  # noqa: ARG001 — reserved for future per-mapping document policy
    document_payload: Mapping[str, str],
    raw: bytes,
    sha256: str,
    storage_ref: str,
    document_repo: DocumentRepository,
    audit: AuditEventStore,
    outbox: OutboxWriter,
    actor_id: UUID,
    tenant_id: UUID,
    correlation_id: str,
    now: Any,
) -> None:
    """Stage document metadata + audit + outbox for already-stored bytes.

    The ``DocumentStore.put`` call lives in ``_create_case_for_row``
    (before any DB write); this helper only stages the metadata row,
    version row, and the ``DocumentAdded`` audit + outbox rows in the
    caller's transaction.
    """
    document = Document.create(
        tenant_id=tenant_id,
        case_id=case.id,
        file_name=document_payload["file_name_field"],
        content_type=document_payload["content_type_field"],
        classification=document_payload["classification_field"],
        sensitivity=DocumentSensitivity.INTERNAL,
        sha256=sha256,
        size=len(raw),
        storage_ref=storage_ref,
        uploaded_by=actor_id,
        now=now,
    )
    version_row = DocumentVersion(
        id=uuid4(),
        tenant_id=tenant_id,
        document_id=document.id,
        version_number=1,
        sha256=sha256,
        size=len(raw),
        storage_ref=storage_ref,
        content_type=document.content_type,
        uploaded_by=actor_id,
        uploaded_at=now,
    )
    await document_repo.add_document_and_version(document=document, version=version_row)
    await audit.append(
        tenant_id=tenant_id,
        event_type=DOCUMENT_ADDED,
        actor_id=actor_id,
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
            "sha256": sha256,
            "size": len(raw),
            "source": "import",
        },
        occurred_at=now,
    )
    await outbox.enqueue(
        tenant_id=tenant_id,
        event_type=DOCUMENT_ADDED,
        event_id=uuid4(),
        payload={
            "case_id": str(case.id),
            "document_id": str(document.id),
            "sha256": sha256,
            "size": len(raw),
            "content_type": document.content_type,
            "classification": document_payload["classification_field"],
            "occurred_at": now.isoformat(),
            "source": "import",
        },
    )
