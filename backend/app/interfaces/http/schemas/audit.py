"""Pydantic request/response schemas for the audit timeline (P09-US1, US4).

The wire shape is the application-layer ``TimelineEntry`` dataclass
serialized verbatim; the router never hand-types fields. The Pydantic
schema exists so the OpenAPI document is honest about every field the
client receives.

``redacted`` is True iff at least one payload value was replaced with
``"***"``. The client renders a badge in that case; it does NOT try to
recover the value (Constitution V — server-side enforcement only).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.interfaces.http.schemas.cases import Page


class TimelineEntryResponse(BaseModel):
    """A single audit row on the timeline wire shape (P09-US1, US4)."""

    id: UUID
    event_type: str
    occurred_at: str
    actor_id: UUID
    actor_type: str
    subject_type: str
    subject_id: UUID
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any] = Field(default_factory=dict)
    hash: str
    prev_hash: str
    config_version: str | None = None
    redacted: bool = False


# ``Page[TimelineEntryResponse]`` is the AGENTS.md §17.1 envelope.
TimelinePage = Page[TimelineEntryResponse]


# ---------------------------------------------------------------------------
# Export + correction wire models (P09-US5, US7; FM-044)
# ---------------------------------------------------------------------------


class ExportEntryResponse(BaseModel):
    """A single audit row in the export batch (P09-US5 / FM-044).

    Mirrors ``TimelineEntryResponse`` and adds the chain anchors the
    export consumer needs to verify integrity off-system. The payload
    is redacted through the same allowlist as the timeline; the
    ``hash`` / ``prev_hash`` fields are integrity metadata, not
    payload content, and are not redacted.
    """

    id: UUID
    event_type: str
    occurred_at: str
    actor_id: UUID
    actor_type: str
    subject_type: str
    subject_id: UUID
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any] = Field(default_factory=dict)
    hash: str
    prev_hash: str
    redacted: bool = False


class ExportBatchResponse(BaseModel):
    """A page of the audit export plus batch metadata (P09-US5 / FM-044)."""

    items: list[ExportEntryResponse]
    next_cursor: str | None
    limit: int = Field(default=50, ge=1, le=200)
    rows_in_batch: int = 0


class CorrectionRequest(BaseModel):
    """Request body for ``POST /api/v1/audit/{event_id}/correction``.

    The operator must pass every field explicitly; the API does not
    default a ``reason`` or ``corrected_summary``.
    """

    reason: str = Field(min_length=1)
    corrected_summary: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)


class CorrectionResponse(BaseModel):
    """Response body for ``POST /api/v1/audit/{event_id}/correction``.

    ``original_event_id`` is the row being annotated; ``new_event_id``
    is the appended ``AuditCorrectionRecorded`` row. The store stamps
    the id at commit; we surface it so a client can correlate the two
    on the timeline.
    """

    original_event_id: UUID
    new_event_id: UUID | None
    correction_payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Access-decision report wire models (P09-US6 / FM-045; P11-US9)
# ---------------------------------------------------------------------------


class AccessDecisionResponse(BaseModel):
    """A single access-decision row on the security report wire shape.

    Carries the evaluated ``action``, the targeted ``resource`` (kind
    + id), the ``actor`` (id + roles at decision time), the policy
    ``result`` (allowed / denied), and the policy ``reason`` so a
    security administrator can answer "who tried what, against what,
    and why".
    """

    id: UUID
    occurred_at: str
    actor_id: UUID
    actor_type: str
    actor_roles: list[str] = Field(default_factory=list)
    action: str
    resource_kind: str
    resource_id: str | None = None
    result: str
    reason: str
    correlation_id: str


class AccessDecisionPage(BaseModel):
    """A page of access-decision rows (P09-US6 / FM-045)."""

    items: list[AccessDecisionResponse]
    next_cursor: str | None
    limit: int = Field(default=50, ge=1, le=200)


# ---------------------------------------------------------------------------
# SIEM stream + signed audit batch wire models (P09-US8, US9 / FM-046;
# ADR-009 — Gated)
# ---------------------------------------------------------------------------


class SignedBatchResponse(BaseModel):
    """A tamper-evident batch of audit rows (P09-US9 / FM-046).

    The auditor re-reads the rows through the standard audit read
    API to verify the chain (P09-US3), then recomputes the signature
    using the published ``key_id`` and ``algorithm`` and compares
    against ``signature``. ``payload`` is the canonical JSON that
    was signed.
    """

    id: UUID
    tenant_id: UUID
    first_event_id: UUID
    last_event_id: UUID
    first_event_hash: str
    last_event_hash: str
    row_count: int = Field(ge=0)
    algorithm: str
    key_id: str
    signature: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    created_by: UUID


class SignedBatchPage(BaseModel):
    """A page of signed batches (P09-US9 / FM-046)."""

    items: list[SignedBatchResponse]
    next_cursor: str | None
    limit: int = Field(default=50, ge=1, le=200)


class SignedBatchRequest(BaseModel):
    """Request body for ``POST /api/v1/audit/signed-batches``.

    The operator picks an inclusive event range (``first_event_id``
    and ``last_event_id``) and provides a free-text ``reason`` that
    is recorded in the batch payload. The chain is verified against
    the durable ``audit_events`` table before signing.
    """

    first_event_id: UUID
    last_event_id: UUID
    reason: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)


__all__ = [
    "AccessDecisionPage",
    "AccessDecisionResponse",
    "CorrectionRequest",
    "CorrectionResponse",
    "ExportBatchResponse",
    "ExportEntryResponse",
    "SignedBatchPage",
    "SignedBatchRequest",
    "SignedBatchResponse",
    "TimelineEntryResponse",
    "TimelinePage",
]
