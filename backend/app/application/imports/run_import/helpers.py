"""Shared stable-string constants and small helpers for the import pipeline (P14 / FM-026)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.application.imports.run_import.commands import (
    CommitImportCommand,
    PreviewImportCommand,
)
from app.application.imports.source_ids import reconciliation_counts
from app.domain.import_job import ImportReconciliation, ImportRowResult

# Stable string constants for the audit + outbox payloads.
SUBJECT_TYPE_IMPORT_JOB = "import_job"
SUBJECT_TYPE_CASE = "case"
SUBJECT_TYPE_DOCUMENT = "document"

# Event types.
IMPORT_PREVIEWED = "ImportPreviewed"
IMPORT_COMMITTED = "ImportCommitted"
IMPORT_ROW_REJECTED = "ImportRowRejected"
DOCUMENT_ADDED = "DocumentAdded"


# Per-row error codes. Stable strings; subscribers and the operator UI
# key on these to bucket rejections.
ERROR_MISSING_TITLE = "missing_title"
ERROR_MISSING_REQUIRED_FIELD = "missing_required_field"
ERROR_UNKNOWN_FIELD = "unknown_field"
ERROR_INVALID_PRIORITY = "invalid_priority"
ERROR_INVALID_ENUM = "invalid_enum"
ERROR_DOCUMENT_CONTENT_MISSING = "document_content_missing"
ERROR_DOCUMENT_CONTENT_INVALID = "document_content_invalid"
ERROR_DOCUMENT_HASH_INVALID = "document_hash_invalid"
ERROR_DOCUMENT_STORAGE_FAILED = "document_storage_failed"
ERROR_DOCUMENT_SIZE_EXCEEDED = "document_size_exceeded"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reconciliation_from(
    results: tuple[ImportRowResult, ...],
) -> ImportReconciliation:
    """Build the reconciliation summary, reusing the pure counter.

    Shares one definition of committed/rejected/skipped with the
    dry-run preview so ``csv`` / ``json`` / ``jsonl`` packages report
    identically; the format string stays provenance-only.
    """
    committed, rejected, skipped = reconciliation_counts(results)
    return ImportReconciliation(
        committed_count=committed,
        rejected_count=rejected,
        skipped_count=skipped,
    )


def _code_for_exception(exc: Exception) -> str:  # noqa: PLR0911 — bucket lookup, not a guard chain
    name = type(exc).__name__
    if name == "ValidationFailed":
        return _code_for_validation_message(str(exc).lower())
    if name == "InvariantViolation":
        return ERROR_MISSING_REQUIRED_FIELD
    return "internal_error"


def _code_for_validation_message(msg: str) -> str:  # noqa: PLR0911 — bucket lookup
    table: tuple[tuple[str, str], ...] = (
        ("priority", ERROR_INVALID_PRIORITY),
        ("enum", ERROR_INVALID_ENUM),
    )
    for needle, code in table:
        if needle in msg:
            return code
    if "document" in msg and "base64" in msg:
        return ERROR_DOCUMENT_CONTENT_INVALID
    if "document" in msg and "missing" in msg:
        return ERROR_DOCUMENT_CONTENT_MISSING
    if "document" in msg and "size" in msg:
        return ERROR_DOCUMENT_SIZE_EXCEEDED
    if "document" in msg and "sha256" in msg:
        return ERROR_DOCUMENT_HASH_INVALID
    return ERROR_MISSING_REQUIRED_FIELD


def _user_context(cmd: PreviewImportCommand | CommitImportCommand) -> Any:
    return _UserProxy(
        tenant_id=cmd.tenant_id,
        user_id=cmd.actor_id,
        roles=cmd.actor_roles,
    )


@dataclass(frozen=True, slots=True)
class _UserProxy:
    """Duck-typed ``UserContext`` for the policy (see create_case)."""

    tenant_id: UUID
    user_id: UUID
    roles: tuple[str, ...]
    display_name: str = ""
    is_system: bool = False


def _now_clock() -> Any:
    """Lazy clock factory used by the dry-run path.

    The preview does not write rows so a fixed clock is sufficient for
    deterministic tests. The commit path receives a real ``Clock`` from
    the caller.
    """

    class _StubClock:
        @staticmethod
        def now() -> Any:  # pragma: no cover — dry-run does not read it
            return datetime(2026, 1, 1, tzinfo=UTC)

    return _StubClock()
