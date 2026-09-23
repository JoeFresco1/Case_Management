"""Pydantic schemas for the admin outbox API (FM-052 / P13-US9 / P12-US4).

Operator surface for the outbox:

- ``GET  /api/v1/admin/outbox``         — lag summary + a page of pending rows.
- ``POST /api/v1/admin/outbox/{id}/replay`` — reset a row so the next
  dispatcher tick re-attempts it.

The schemas are the OpenAPI contract; hand-edits outside this file are
forbidden (AGENTS.md §4.R7).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class OutboxLagSummary(BaseModel):
    """Lag counters for the operator dashboard.

    ``pending`` is everything with no ``dispatched_at``; ``dead_lettered``
    is the subset that has hit ``MAX_ATTEMPTS`` and will not be retried
    automatically; ``retrying`` is rows with a non-null ``last_error``
    but still under the cap.
    """

    total: int = Field(ge=0, description="All outbox rows in scope")
    pending: int = Field(ge=0, description="Rows not yet dispatched")
    dead_lettered: int = Field(
        ge=0,
        description="Rows at MAX_ATTEMPTS — auto-retry exhausted",
    )
    retrying: int = Field(
        ge=0,
        description="Pending rows with a recorded failure",
    )


class OutboxRowResponse(BaseModel):
    """Wire shape of a single outbox row.

    The row id is the operator-facing key (it appears in the replay
    path). ``is_dead_lettered`` is a derived flag the UI uses to color
    the row red — the dispatcher computes it from ``attempt_count``
    rather than persisting it on the row.
    """

    id: UUID
    tenant_id: UUID
    event_type: str
    event_id: UUID
    created_at: datetime
    dispatched_at: datetime | None
    attempt_count: int = Field(ge=0)
    last_error: str | None
    is_dead_lettered: bool


class OutboxListResponse(BaseModel):
    """Wrapper for the list endpoint.

    Mirrors the canonical ``Page[T]`` envelope (AGENTS.md §17.1) so the
    operator UI can paginate via the same client.
    """

    items: list[OutboxRowResponse]
    limit: int
    lag: OutboxLagSummary


class OutboxReplayResponse(BaseModel):
    """Result of ``POST /admin/outbox/{id}/replay``.

    ``reset`` is the success path; ``noop`` means the row did not
    exist or was already dispatched (the operator UI distinguishes
    them so the caller's intent — "retry this row" — is observable.
    """

    id: UUID
    reset: bool
