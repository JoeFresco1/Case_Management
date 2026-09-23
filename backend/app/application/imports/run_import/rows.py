"""Per-row evaluation: translate one source row to a target case payload (P14-US2)."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from typing import Any

from app.application.imports.run_import.commands import ImportRow
from app.application.imports.run_import.helpers import (
    ERROR_DOCUMENT_CONTENT_INVALID,
    ERROR_DOCUMENT_HASH_INVALID,
    ERROR_DOCUMENT_SIZE_EXCEEDED,
    ERROR_MISSING_REQUIRED_FIELD,
    ERROR_MISSING_TITLE,
    _code_for_exception,
)
from app.domain.case_type import CaseTypeVersion
from app.domain.document import is_valid_sha256
from app.domain.errors import InvariantViolation, ValidationFailed
from app.domain.import_job import (
    DocumentMapping,
    ImportRowOutcome,
    ImportRowResult,
    MappingVersion,
)

# ---------------------------------------------------------------------------
# Per-row evaluation
# ---------------------------------------------------------------------------


def _evaluate_row(
    row: ImportRow,
    mapping: MappingVersion,
    version: CaseTypeVersion,
) -> ImportRowResult:
    """Validate a single row against the mapping + case-type schema.

    Returns a rejected result on any failure. Never raises — a rejected
    row is part of the reconciliation report, not a pipeline error.
    """
    try:
        case_data, title, description, priority, document_payload = _build_row(
            row, mapping, version
        )
    except (ValidationFailed, InvariantViolation) as exc:
        return ImportRowResult(
            row_index=row.index,
            outcome=ImportRowOutcome.REJECTED,
            error_code=_code_for_exception(exc),
            error_message=str(exc),
        )

    # The case aggregate validates the final payload; we mirror that
    # validation here so the preview matches the commit's outcome.
    required_keys = version.schema.required_field_keys()
    missing = [k for k in required_keys if not case_data.get(k)]
    if missing:
        return ImportRowResult(
            row_index=row.index,
            outcome=ImportRowOutcome.REJECTED,
            error_code=ERROR_MISSING_REQUIRED_FIELD,
            error_message=f"required fields missing: {sorted(missing)}",
        )

    # Document policy check (size + classification + content-type).
    if mapping.documents is not None and document_payload is not None:
        doc_check = _check_document_policy(mapping.documents, document_payload, version)
        if doc_check is not None:
            return ImportRowResult(
                row_index=row.index,
                outcome=ImportRowOutcome.REJECTED,
                error_code=doc_check[0],
                error_message=doc_check[1],
            )

    # The row's title (separately from data) is required.
    if not title:
        return ImportRowResult(
            row_index=row.index,
            outcome=ImportRowOutcome.REJECTED,
            error_code=ERROR_MISSING_TITLE,
            error_message="title field is required",
        )

    return ImportRowResult(
        row_index=row.index,
        outcome=ImportRowOutcome.ACCEPTED,
    )


def _build_row(
    row: ImportRow,
    mapping: MappingVersion,
    version: CaseTypeVersion,
) -> tuple[dict[str, Any], str, str, str, dict[str, str] | None]:
    """Translate a source row to a target ``case.data`` payload.

    Returns ``(data, title, description, priority, document_payload)``.
    Raises :class:`ValidationFailed` on any field-level failure so the
    caller can map it to a stable error code.

    Only fields the case type's schema declares are written to
    ``case.data``; the import pipeline is a capture channel, not a
    schema-evolution channel. A column that maps to a field the type
    does not declare (e.g. an external ``request_id`` we want to keep
    for traceability) is silently dropped from ``case.data`` — the
    column name still appears in the audit payload via
    ``ImportCommitted`` so operators can trace the import.
    """
    schema_field_keys = set(version.schema.field_keys())
    data: dict[str, Any] = {}
    for fm in mapping.fields:
        value = row.values.get(fm.source)
        if value is None or value == "":
            # Empty values are skipped — required-field enforcement
            # happens later, on the merged payload.
            continue
        if fm.target not in schema_field_keys:
            # Drop unmapped fields rather than fail the import; the
            # schema is the contract, not the mapping.
            continue
        data[fm.target] = value

    # Title / description / priority live on the case aggregate, not on
    # ``case.data``. We extract them here so the per-row policy can
    # enforce required / enum values before the case is constructed.
    title = row.values.get(mapping.title_field, "") if mapping.title_field else ""
    description = row.values.get(mapping.description_field, "") if mapping.description_field else ""
    priority = (
        row.values.get(mapping.priority_field, "normal") if mapping.priority_field else "normal"
    )
    if priority not in ("low", "normal", "high", "urgent"):
        raise ValidationFailed(f"invalid priority value: {priority!r}", field="priority")

    # Enum / multichoice fields: validate against the schema's declared
    # values. A multichoice value is comma-separated in the source.
    for f in version.schema.fields:
        if f.type not in {"enum", "multichoice"}:
            continue
        if f.key not in data:
            continue
        raw = data[f.key]
        chosen = [v for v in str(raw).split(",") if v]
        invalid = [v for v in chosen if v not in f.enum_values]
        if invalid:
            raise ValidationFailed(
                f"invalid enum value(s) for {f.key}: {sorted(invalid)}",
                field=f.key,
            )

    document_payload: dict[str, str] | None = None
    if mapping.documents is not None:
        document_payload = _read_document_payload(mapping.documents, row)

    return data, title.strip(), description, priority, document_payload


def _read_document_payload(documents: DocumentMapping, row: ImportRow) -> dict[str, str]:
    """Read the document columns from the row, validating shape.

    Raises :class:`ValidationFailed` if a column is missing or
    malformed. The returned dict is consumed by the commit path's
    storage / hash / size checks.
    """
    out: dict[str, str] = {}
    missing: list[str] = []
    for attr in (
        "file_name_field",
        "content_field",
        "content_type_field",
        "classification_field",
        "sha256_field",
    ):
        column = getattr(documents, attr)
        value = row.values.get(column, "")
        if not value:
            missing.append(column)
            continue
        out[attr] = value
    if missing:
        raise ValidationFailed(
            f"document columns missing: {sorted(missing)}",
            field="document",
        )
    return out


def _check_document_policy(
    documents: DocumentMapping,  # noqa: ARG001 — reserved for future document_classifications iteration
    payload: Mapping[str, str],
    version: CaseTypeVersion,
) -> tuple[str, str] | None:
    """Validate a document payload against the case-type policy.

    Returns ``(error_code, message)`` on failure or ``None`` on success.
    """
    classification = payload["classification_field"]
    if not version.schema.has_classification(classification):
        return (
            ERROR_DOCUMENT_CONTENT_INVALID,
            f"classification {classification!r} is not permitted for this case type",
        )

    sha = payload["sha256_field"]
    if not is_valid_sha256(sha):
        return (
            ERROR_DOCUMENT_HASH_INVALID,
            f"sha256 {sha!r} is not a valid lowercase hex digest",
        )

    content_b64 = payload["content_field"]
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        return (
            ERROR_DOCUMENT_CONTENT_INVALID,
            f"document content is not valid base64: {exc!s}",
        )

    max_size = version.schema.document_max_size_bytes or 25 * 1024 * 1024
    if len(raw) > max_size:
        return (
            ERROR_DOCUMENT_SIZE_EXCEEDED,
            f"document size {len(raw)} exceeds case-type maximum {max_size}",
        )
    return None
