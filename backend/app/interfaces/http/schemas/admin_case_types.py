"""Pydantic request/response schemas for the admin case-types API.

Schema shape mirrors the canonical ``CaseTypeSchema`` (see
``app/domain/case_type.py``). The HTTP layer is intentionally a
**projection**: it does not invent fields the domain does not already
own. This keeps the wire format compatible with the long-lived
declarative contract from AGENTS.md §16.2.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# Pillar P05 §2: the v1 field-type set is closed. Any new type must be
# added here AND in ``_FIELD_TYPES`` in ``domain/case_type.py``.
FieldType = Literal[
    "text",
    "longtext",
    "integer",
    "decimal",
    "boolean",
    "date",
    "datetime",
    "enum",
    "multichoice",
    "reference",
    "json",
]

SensitivityLevel = Literal[
    "public",
    "internal",
    "confidential",
    "restricted",
    "phi",
]

CaseTypeVersionStatus = Literal["draft", "published", "retired"]


class FieldDefinitionRequest(BaseModel):
    """Wire shape of a single field on a CaseTypeVersion schema."""

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    type: FieldType
    required: bool = False
    enum_values: list[str] = Field(default_factory=list)
    sensitivity: SensitivityLevel = "internal"
    searchable: bool = False


class CaseTypeSchemaRequest(BaseModel):
    """The full ``schema_json`` body for a draft version.

    Validated by the application-layer validator; if it fails, the
    request returns 422 ``validation_failed`` and the row is not
    created.
    """

    fields: list[FieldDefinitionRequest] = Field(default_factory=list)
    document_classifications: list[str] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)


class DraftCaseTypeVersionRequest(BaseModel):
    """Body for ``POST /api/v1/admin/case-types/{key}/versions``."""

    schema_: CaseTypeSchemaRequest = Field(alias="schema")

    model_config = {"populate_by_name": True}


class CaseTypeVersionSummary(BaseModel):
    """Per-version entry returned by the history endpoint.

    Excludes the full ``schema_json`` blob so the admin UI can render
    the publication timeline without downloading every version's field
    list. The full schema is fetchable by id.
    """

    id: UUID
    version_number: int
    status: CaseTypeVersionStatus
    published_at: str | None
    published_by: UUID | None
    created_at: str
    field_keys: list[str]
    document_classifications: list[str]


class PublicationHistoryResponse(BaseModel):
    case_type_id: UUID
    versions: list[CaseTypeVersionSummary]


class CaseTypeVersionDetail(BaseModel):
    """Full inspect view of one CaseTypeVersion (FM-021 inspect).

    Why separate from ``CaseTypeVersionSummary``: the history list
    carries only the fingerprint (field keys) so the timeline stays
    light; the inspect view carries the full ``schema`` blob plus the
    authorship/effective-time lineage (``published_by`` author,
    ``published_at`` effective time, ``created_at`` draft time) so an
    admin can answer "who authored what, when did it take effect,
    and what exactly changed" without a second join. The approval
    chain (second-admin ``validate`` row + ``change_summary``) lives
    on the governance timeline ``GET /admin/publications`` keyed by
    the same ``version_id``.
    """

    id: UUID
    case_type_id: UUID
    version_number: int
    status: CaseTypeVersionStatus
    published_at: str | None
    published_by: UUID | None
    created_at: str
    field_keys: list[str]
    document_classifications: list[str]
    schema_: CaseTypeSchemaRequest = Field(alias="schema")

    model_config = {"populate_by_name": True}


class PublishResultResponse(BaseModel):
    case_type_id: UUID
    case_type_version_id: UUID
    version_number: int
    status: CaseTypeVersionStatus


class DraftResultResponse(BaseModel):
    case_type_id: UUID
    case_type_version_id: UUID
    version_number: int
    status: CaseTypeVersionStatus


__all__ = [
    "CaseTypeSchemaRequest",
    "CaseTypeVersionDetail",
    "CaseTypeVersionStatus",
    "CaseTypeVersionSummary",
    "DraftCaseTypeVersionRequest",
    "DraftResultResponse",
    "FieldDefinitionRequest",
    "FieldType",
    "PublicationHistoryResponse",
    "PublishResultResponse",
    "SensitivityLevel",
]
