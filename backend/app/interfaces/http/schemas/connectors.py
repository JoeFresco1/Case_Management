"""HTTP schemas for the connectors API (P14-US5, US7 / FM-058).

The schemas are the OpenAPI contract (AGENTS.md §4.R7). They mirror
the application-layer dataclasses but do not import them — the HTTP
boundary translates wire shapes into use-case inputs and use-case
outputs back into wire shapes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DraftConnectorRequest(BaseModel):
    """Body of ``POST /api/v1/admin/connectors``."""

    key: str = Field(min_length=1, max_length=64)
    kind: str = Field(
        description="synthetic | onbase | panzura (named kinds are Gated per ADR-015)"
    )
    design_summary: str = Field(min_length=1, max_length=2000)
    authorization_rationale: str = Field(
        default="",
        max_length=2000,
        description=(
            "Required for named connectors (any kind other than 'synthetic'); "
            "the domain refuses to construct the row without it. Optional "
            "for synthetic connectors but recommended."
        ),
    )


class ConnectorResponse(BaseModel):
    """Wire shape of a :class:`Connector` aggregate."""

    id: str
    tenant_id: str
    key: str
    kind: str
    state: str
    adapter_ref: str
    design_summary: str
    named: bool
    is_terminal: bool
    accepts_traffic: bool
    in_flight: int
    completed: int
    rejected: int
    drain_confirmed: bool
    created_at: str
    updated_at: str
    created_by: str
    approved_at: str | None = None
    approved_by: str | None = None
    enabled_at: str | None = None
    enabled_by: str | None = None
    retired_at: str | None = None
    retired_by: str | None = None
    authorization_rationale: str = ""
    implementation_authorized_by: str | None = None


class DraftConnectorResponse(BaseModel):
    connector: ConnectorResponse


class ApproveConnectorRequest(BaseModel):
    """Body of ``POST /api/v1/admin/connectors/{id}/approve``."""

    rationale: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "Non-empty rationale recorded on the row and the audit payload. "
            "The domain refuses to approve without it."
        ),
    )


class ApproveConnectorResponse(BaseModel):
    connector: ConnectorResponse


class EnableConnectorResponse(BaseModel):
    connector: ConnectorResponse


class MonitorConnectorResponse(BaseModel):
    """Wire shape of the monitor snapshot."""

    connector: ConnectorResponse
    in_flight: int
    completed: int
    rejected: int
    accepts_traffic: bool
    is_terminal: bool


class RetireConnectorRequest(BaseModel):
    """Body of ``POST /api/v1/admin/connectors/{id}/retire``."""

    drain_notes: str = Field(
        default="",
        max_length=2000,
        description="Operator's plan / context for the drain.",
    )
    in_flight: int = Field(
        ge=0,
        description=(
            "Operator-confirmed in-flight count. The use case refuses to "
            "retire while in_flight > 0 (Constitution V)."
        ),
    )
    completed: int = Field(ge=0)
    rejected: int = Field(ge=0)


class RetireConnectorResponse(BaseModel):
    connector: ConnectorResponse
    drained: bool
