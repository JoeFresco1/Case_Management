"""Pydantic schemas for the admin import-mapping API (FM-026 / P14-US3).

The schemas mirror the application-layer dataclasses but do not import
them — the HTTP boundary translates wire shapes into use-case inputs and
use-case outputs back into wire shapes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DraftImportMappingRequest(BaseModel):
    """Body for ``POST /admin/import-mappings``."""

    key: str = Field(min_length=1, max_length=64)
    mapping_json: dict[str, Any] = Field(
        ..., description="Mapping document in MappingVersion shape"
    )


class DraftImportMappingResponse(BaseModel):
    """Response of a draft/publish/retire operation."""

    mapping_id: UUID
    version_number: int
    status: str


class ImportMappingListItem(BaseModel):
    """List item for import mappings."""

    id: UUID
    key: str
    version_number: int
    status: str
    published_at: str | None = None
    published_by: UUID | None = None
    created_at: str


class ImportMappingDetailResponse(BaseModel):
    """Full detail of one import mapping version."""

    id: UUID
    tenant_id: UUID
    key: str
    version_number: int
    mapping_json: dict[str, Any]
    status: str
    published_at: str | None = None
    published_by: UUID | None = None
    created_at: str


__all__ = [
    "DraftImportMappingRequest",
    "DraftImportMappingResponse",
    "ImportMappingDetailResponse",
    "ImportMappingListItem",
]
