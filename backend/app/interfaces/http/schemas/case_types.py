"""Explicit response schemas for the intake case-type catalog (T051)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.interfaces.http.schemas.admin_case_types import FieldType, SensitivityLevel


class PublishedCaseTypeField(BaseModel):
    """A supported, published field definition rendered by intake forms."""

    key: str
    label: str
    type: FieldType
    required: bool = False
    enum_values: list[str] = Field(default_factory=list)
    sensitivity: SensitivityLevel = "internal"
    searchable: bool = False


class PublishedCaseTypeSchema(BaseModel):
    """The declarative schema needed to render and validate intake."""

    fields: list[PublishedCaseTypeField] = Field(default_factory=list)
    document_classifications: list[str] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)


class PublishedCaseTypeResponse(BaseModel):
    """One published case type usable by the authenticated intake actor."""

    model_config = ConfigDict(protected_namespaces=())

    id: UUID
    key: str
    name: str
    description: str
    sensitivity_default: str
    version_id: UUID
    version_number: int
    schema: PublishedCaseTypeSchema


class PublishedCaseTypeListResponse(BaseModel):
    """Tenant-scoped published case types for the create-case selector."""

    items: list[PublishedCaseTypeResponse]
    next_cursor: str | None = None
    limit: int


__all__ = [
    "PublishedCaseTypeField",
    "PublishedCaseTypeListResponse",
    "PublishedCaseTypeResponse",
    "PublishedCaseTypeSchema",
]
