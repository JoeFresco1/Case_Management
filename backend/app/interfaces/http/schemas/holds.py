"""Pydantic schemas for the legal-hold HTTP surface (FM-039).

The schema is the OpenAPI contract — never hand-edit at the router.
New fields land here first, then in the use case; the router only
translates between Pydantic and the application layer.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class PlaceHoldRequest(BaseModel):
    """Request body for ``POST /api/v1/cases/{id}/holds``.

    ``reason`` is required so the audit row never carries a
    meaningless rationale. The HTTP layer maps ``ValidationFailed``
    on a blank reason to 422.
    """

    reason: str = Field(
        min_length=1,
        max_length=1024,
        description="Free-text rationale for placing the hold (required).",
    )


class ReleaseHoldRequest(BaseModel):
    """Request body for ``DELETE /api/v1/cases/{id}/holds``.

    ``reason`` is required for the same reason as
    :class:`PlaceHoldRequest` — every release carries a rationale
    so an auditor can replay the chain and see why the disposition
    block was lifted.
    """

    reason: str = Field(
        min_length=1,
        max_length=1024,
        description="Free-text rationale for releasing the hold (required).",
    )


class LegalHoldResponse(BaseModel):
    """The wire shape of a :class:`app.domain.legal_hold.LegalHold` row.

    The router serializes the domain aggregate into this shape so
    the client sees a stable, Pydantic-typed contract.
    """

    id: UUID
    tenant_id: UUID
    case_id: UUID
    placed_by: UUID
    placed_reason: str
    placed_at: datetime
    released_by: UUID | None = None
    released_reason: str | None = None
    released_at: datetime | None = None
    is_active: bool


__all__ = [
    "LegalHoldResponse",
    "PlaceHoldRequest",
    "ReleaseHoldRequest",
]
