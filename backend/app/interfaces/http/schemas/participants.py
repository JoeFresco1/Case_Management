"""OpenAPI schemas for case participants."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ParticipantRequest(BaseModel):
    """Participant metadata accepted on create."""

    role: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=64)
    external_ref: str | None = None
    is_primary: bool = False


class ParticipantPatch(BaseModel):
    """Mutable participant metadata."""

    role: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    source: str | None = Field(default=None, min_length=1, max_length=64)
    external_ref: str | None = None
    is_primary: bool | None = None


class ParticipantResponse(BaseModel):
    """Participant including its logical-removal timestamp."""

    id: UUID
    tenant_id: UUID
    case_id: UUID
    role: str
    display_name: str
    source: str
    external_ref: str | None = None
    is_primary: bool
    added_at: datetime
    removed_at: datetime | None = None
