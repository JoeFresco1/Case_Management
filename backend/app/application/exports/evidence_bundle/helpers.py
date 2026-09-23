"""Validation, redaction, projection, and clock helpers for the evidence bundle.

Small pure helpers shared by the bundle's use case: command validation,
the canonical-JSON/SHA-256 codecs that make the manifest hash stable,
the two redaction layers (case data through the case-type schema, audit
payloads through the timeline allowlist), JSON-safe projections of the
case/document/audit rows, the repository hook that lists a case's
documents, and the clock adapters. Split out of the former single
``evidence_bundle`` module by the R2 structural refactor; everything
here is re-exported from ``app.application.exports.evidence_bundle``
unchanged.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.application.audit import view_timeline as view_timeline_uc
from app.application.cases.redactor import redact_case_data
from app.domain.case import Case
from app.domain.document import Document, DocumentSensitivity
from app.domain.errors import ValidationFailed
from app.domain.ports import DocumentRepository

from .types import BuildEvidenceBundleCommand

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ActorProxy:
    """Duck-typed ``UserContext`` for the policy."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _validate_command(cmd: BuildEvidenceBundleCommand) -> None:
    """Run the application-level invariants on ``cmd``.

    A missing reason is refused with a structured
    :class:`ValidationFailed` so the HTTP layer can render a 422
    with a ``field="reason"`` detail. The reason is part of the
    audit event's payload and lands on the chain verbatim; an empty
    reason would degrade the audit trail.
    """
    if not cmd.reason or not cmd.reason.strip():
        raise ValidationFailed(
            "reason is required for an evidence bundle export",
            field="reason",
        )


def _canonical_json(payload: dict[str, Any]) -> str:
    """Stable JSON encoding used for hashing.

    Mirrors :func:`app.domain.audit.canonical_json` so the bundle
    manifest's hash uses the same codec the audit chain uses.
    Identical payloads must hash identically regardless of dict
    insertion order or whitespace.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_hex(data: bytes) -> str:
    """Hex digest of ``data``.

    The lower-case 64-char form matches the audit chain's
    ``compute_hash`` shape so a single tool can verify both the
    chain anchors and the archive's content hashes.
    """
    return hashlib.sha256(data).hexdigest()


def _redact_case_data(case: Case, schema: Any, can_see_sensitive: bool) -> dict[str, Any]:
    """Redact the case's ``data`` payload through the case-type schema.

    Uses :func:`app.application.cases.redactor.redact_case_data` so
    the redaction semantics are identical to the inline case view.
    When ``can_see_sensitive`` is True the schema's restricted
    fields pass through untouched; otherwise their values are
    replaced with the standard :class:`RedactedField` marker.
    """
    return redact_case_data(
        data=case.data,
        schema=schema,
        can_see_sensitive=can_see_sensitive,
    )


def _redact_audit_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Apply the timeline allowlist to an audit row's payload.

    Mirrors :func:`app.application.audit.export_audit._redact_payload`
    so the bundle's audit section is no more revealing than the
    structured audit export. Returns the redacted payload and a flag
    indicating whether any value was replaced.
    """
    safe = view_timeline_uc.PAYLOAD_SAFE_KEYS
    redacted = False
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k in safe:
            out[k] = v
            continue
        redacted = True
        out[k] = "***"
    return out, redacted


def _case_to_dict(case: Case, redacted_data: dict[str, Any]) -> dict[str, Any]:
    """Translate the case aggregate into a JSON-safe dict.

    Why a translator: the case aggregate uses UUID and datetime
    values that the canonical JSON encoder does not know about. The
    function produces a stable shape so a downstream tool can
    re-hydrate the case (or at least compare it) without depending
    on the platform's internal type system.
    """
    redacted_fields = sorted(
        k for k in case.data if k not in redacted_data or redacted_data[k] != case.data[k]
    )
    return {
        "id": str(case.id),
        "tenant_id": str(case.tenant_id),
        "reference": case.reference.value,
        "case_type_version_id": str(case.case_type_version_id),
        "status": case.status.value,
        "priority": case.priority,
        "title": case.title,
        "description": case.description,
        "data": redacted_data,
        "current_workflow_state": case.current_workflow_state,
        "version_token": case.version_token,
        "sensitivity": case.sensitivity,
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
        "created_by": str(case.created_by),
        "closed_at": case.closed_at.isoformat() if case.closed_at else None,
        "redacted_fields": redacted_fields,
    }


def _document_to_dict(
    document: Document,
    *,
    content_sha256: str,
    content_size: int,
) -> dict[str, Any]:
    """Translate a Document into a JSON-safe dict for the archive.

    ``content_sha256`` and ``content_size`` come from the
    ``DocumentStore.get`` call so the manifest can record what was
    actually read; if the store and the row disagree, the manifest
    exposes the discrepancy.
    """
    return {
        "id": str(document.id),
        "tenant_id": str(document.tenant_id),
        "case_id": str(document.case_id),
        "file_name": document.file_name,
        "content_type": document.content_type,
        "classification": document.classification,
        "sensitivity": document.sensitivity.value
        if isinstance(document.sensitivity, DocumentSensitivity)
        else str(document.sensitivity),
        "sha256": content_sha256,
        "size": content_size,
        "current_version": document.current_version,
        "locked": document.locked,
        "locked_at": document.locked_at.isoformat() if document.locked_at else None,
        "locked_by": str(document.locked_by) if document.locked_by else None,
        "uploaded_by": str(document.uploaded_by),
        "created_at": document.created_at.isoformat(),
        "updated_at": document.updated_at.isoformat(),
    }


def _audit_row_to_dict(row: Any) -> dict[str, Any]:
    """Translate a persistence row into the bundle's audit entry shape.

    The chain anchors (``hash`` / ``prev_hash``) and the
    chronological position are preserved so a downstream auditor
    can replay the chain with the standard
    :func:`app.domain.audit.verify_chain` function.
    """
    redacted_payload, _ = _redact_audit_payload(dict(row.payload or {}))
    return {
        "id": str(row.id),
        "event_type": row.event_type,
        "occurred_at": row.occurred_at.isoformat(),
        "actor_id": str(row.actor_id),
        "actor_type": row.actor_type,
        "subject_type": row.subject_type,
        "subject_id": str(row.subject_id),
        "correlation_id": row.correlation_id,
        "causation_id": row.causation_id,
        "payload": redacted_payload,
        "hash": row.hash,
        "prev_hash": row.prev_hash,
    }


async def _list_documents_for_case(
    repo: DocumentRepository, tenant_id: UUID, case_id: UUID
) -> list[Document]:
    """Return every document the repository knows for ``(tenant, case)``.

    Mirrors the GraphQL governed surface's
    :func:`app.application.documents.list_documents._list_documents_for_case`
    helper so the bundle and the inline read path share the same
    repository contract. The repository is duck-typed so a missing
    ``list_for_case`` surfaces as :class:`NotImplementedError`
    rather than a static type-check failure.
    """
    method = getattr(repo, "list_for_case", None)
    if method is None:
        raise NotImplementedError(
            "DocumentRepository must implement list_for_case for the "
            "evidence bundle export (FM-060)"
        )
    result = await method(tenant_id=tenant_id, case_id=case_id)
    return list(result)


def _now(clock: Any | None) -> datetime:
    """Return ``clock.now()`` when a clock is wired, else UTC now.

    Mirrors the convention in :mod:`app.application.audit.export_audit`
    so the use case is testable without a clock dependency.
    """
    if clock is None:
        return datetime.now(tz=UTC)
    result = clock.now()
    assert isinstance(result, datetime)
    return result


def _now_iso(clock: Any | None) -> str:
    """Return the export timestamp in canonical ISO-8601 form."""
    return _now(clock).isoformat()


__all__ = [
    "Any",
    "BuildEvidenceBundleCommand",
    "Case",
    "Document",
    "DocumentRepository",
    "DocumentSensitivity",
    "UTC",
    "UUID",
    "ValidationFailed",
    "_ActorProxy",
    "_audit_row_to_dict",
    "_canonical_json",
    "_case_to_dict",
    "_document_to_dict",
    "_list_documents_for_case",
    "_now",
    "_now_iso",
    "_redact_audit_payload",
    "_redact_case_data",
    "_sha256_hex",
    "_validate_command",
    "dataclass",
    "datetime",
    "hashlib",
    "json",
    "redact_case_data",
    "view_timeline_uc",
]
