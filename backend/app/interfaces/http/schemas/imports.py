"""HTTP schemas for the imports API (P14-US2, US3, US4 / P04-US3).

The schemas are the OpenAPI contract (AGENTS.md §4.R7). They mirror
the application-layer dataclasses but do not import them — the HTTP
boundary translates wire shapes into use-case inputs and use-case
outputs back into wire shapes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.import_job import ImportJobStatus


class FieldMappingRequest(BaseModel):
    """Wire shape of a single ``FieldMapping``."""

    source: str = Field(..., description="Source column name in the input row")
    target: str = Field(..., description="Target case-type field key")


class DocumentMappingRequest(BaseModel):
    """Wire shape of a ``DocumentMapping``."""

    file_name_field: str
    content_field: str
    content_type_field: str
    classification_field: str
    sha256_field: str


class MappingVersionRequest(BaseModel):
    """Wire shape of a versioned mapping document.

    The version number is informational on input — the application
    layer validates the rest of the document against the case-type
    schema before accepting.
    """

    version: int = Field(ge=1, description="Mapping version number (>=1)")
    case_type_key: str = Field(..., description="Target case-type key")
    fields: list[FieldMappingRequest]
    title_field: str | None = None
    description_field: str | None = None
    priority_field: str | None = None
    source_id_field: str | None = Field(
        default=None,
        description="Source column carrying the stable source-record id "
        "(enables duplicate detection + idempotent re-submission)",
    )
    documents: DocumentMappingRequest | None = None


class ImportRowRequest(BaseModel):
    """A single source row.

    ``index`` is the operator-facing ordinal (1-based). ``values`` maps
    source column names to string values.
    """

    index: int = Field(ge=1)
    values: dict[str, str]


class ImportRowsPayload(BaseModel):
    """The body of a dry-run or commit request."""

    source_label: str = Field(..., max_length=200)
    source_format: str = Field(..., description="csv | json | jsonl")
    mapping: MappingVersionRequest | None = None
    mapping_id: UUID | None = Field(
        default=None,
        description="Reference a published ImportMapping by id (FM-026). "
        "Use this instead of inline 'mapping' when the mapping is "
        "registered in the import-mapping registry.",
    )
    rows: list[ImportRowRequest]


class ImportRowResponse(BaseModel):
    """Wire shape of a per-row reconciliation entry."""

    row_index: int
    outcome: str
    case_id: str | None = None
    source_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class ImportReconciliationResponse(BaseModel):
    """Wire shape of the reconciliation summary."""

    committed_count: int
    rejected_count: int
    skipped_count: int = 0


class ImportJobResponse(BaseModel):
    """Wire shape of the persisted import job.

    The status is exposed so the operator UI can branch on
    ``previewed`` / ``committed`` / ``failed``.
    """

    id: str
    tenant_id: str
    source_label: str
    source_format: str
    source_row_count: int
    status: ImportJobStatus
    mapping: dict[str, Any]
    results: list[ImportRowResponse]
    reconciliation: ImportReconciliationResponse | None = None
    created_at: str
    committed_at: str | None = None
    failure_reason: str | None = None
    correlation_id: str


class ImportJobListItemResponse(BaseModel):
    """Minimal job summary for the list endpoint (FM-027)."""

    id: str
    source_label: str
    source_format: str
    source_row_count: int
    status: ImportJobStatus
    created_by: str
    created_at: str
    committed_at: str | None = None


class PreviewResponse(BaseModel):
    """Response of ``POST /api/v1/imports/dry-run``."""

    job: ImportJobResponse


class CommitResponse(BaseModel):
    """Response of ``POST /api/v1/imports/commit``."""

    job: ImportJobResponse
    created_case_ids: list[str]


__all__ = [
    "CommitResponse",
    "DocumentMappingRequest",
    "FieldMappingRequest",
    "ImportJobListItemResponse",
    "ImportJobResponse",
    "ImportReconciliationResponse",
    "ImportRowRequest",
    "ImportRowResponse",
    "ImportRowsPayload",
    "MappingVersionRequest",
    "PreviewResponse",
]
