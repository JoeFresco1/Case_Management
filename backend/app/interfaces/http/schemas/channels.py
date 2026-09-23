"""HTTP schemas for the channels + quarantine API (P04-US4, US5 / FM-027).

The schemas are the OpenAPI contract (AGENTS.md §4.R7). They mirror
the application-layer dataclasses but do not import them — the HTTP
boundary translates wire shapes into use-case inputs and use-case
outputs back into wire shapes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ValidationPolicyRequest(BaseModel):
    """Wire shape of a :class:`ValidationPolicy`."""

    required_fields: list[str] = Field(default_factory=list)
    allowed_classifications: list[str] = Field(default_factory=list)
    max_record_bytes: int | None = None
    missing_field_severity: str = Field(default="quarantine")
    unknown_field_severity: str = Field(default="quarantine")
    invalid_enum_severity: str = Field(default="quarantine")


class EnableChannelRequest(BaseModel):
    """Body of ``POST /api/v1/admin/channels`` or ``PUT .../{id}``."""

    key: str = Field(min_length=1, max_length=64)
    kind: str = Field(description="import | api | file_upload")
    policy: ValidationPolicyRequest = Field(default_factory=ValidationPolicyRequest)
    enabled: bool = True
    channel_id: str | None = Field(
        default=None,
        description="If supplied, update the existing channel instead of creating one",
    )


class ChannelResponse(BaseModel):
    id: str
    tenant_id: str
    key: str
    kind: str
    enabled: bool
    policy: dict[str, Any]
    created_at: str
    updated_at: str
    created_by: str


class EnableChannelResponse(BaseModel):
    channel: ChannelResponse
    created: bool


class DisableChannelResponse(BaseModel):
    channel: ChannelResponse


class QuarantineCaptureRequest(BaseModel):
    """Body of ``POST /api/v1/admin/channels/{id}/captures``."""

    source_label: str = Field(min_length=1, max_length=200)
    values: dict[str, str] = Field(default_factory=dict)


class QuarantineRecordResponse(BaseModel):
    id: str
    tenant_id: str
    channel_id: str
    channel_key: str
    source_label: str
    captured_at: str
    captured_by: str
    error_code: str
    error_message: str
    correlation_id: str
    status: str
    resolved_at: str | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class QuarantineCaptureResponse(BaseModel):
    accepted: bool
    error_code: str | None = None
    error_message: str | None = None
    record: QuarantineRecordResponse | None = None


class InspectQuarantineResponse(BaseModel):
    items: list[QuarantineRecordResponse]
    total: int


class ResolveQuarantineRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class ResolveQuarantineResponse(BaseModel):
    record: QuarantineRecordResponse


__all__ = [
    "ChannelResponse",
    "DisableChannelResponse",
    "EnableChannelRequest",
    "EnableChannelResponse",
    "InspectQuarantineResponse",
    "QuarantineCaptureRequest",
    "QuarantineCaptureResponse",
    "QuarantineRecordResponse",
    "ResolveQuarantineRequest",
    "ResolveQuarantineResponse",
    "ValidationPolicyRequest",
]
