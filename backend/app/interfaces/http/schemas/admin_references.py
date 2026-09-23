"""Pydantic schemas for the reference-format admin API (FM-009 / P01-US9).

The wire shape is intentionally a thin projection of the domain
entity (``app.domain.reference_format.ReferenceFormat``); it does not
introduce fields the domain does not already own. This keeps the
declarative contract aligned with AGENTS.md §16 — the schema layer
is a translation surface, not a second source of truth.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class ConfigureReferenceFormatRequest(BaseModel):
    """Body for ``POST /api/v1/admin/reference-format``.

    The ``template`` is the **human-readable** projection only; the
    Case's internal UUID identity is unchanged by this endpoint
    (Constitution I). Validation runs in the application layer
    (``validate_template``) so a malformed template yields
    ``422 validation_failed`` with a structured error body and is
    never persisted.
    """

    template: str = Field(min_length=1, max_length=200)


class ReferenceFormatResponse(BaseModel):
    """Body for ``GET`` / ``POST`` / ``DELETE`` on the admin reference-format API."""

    tenant_id: UUID
    template: str
    is_default: bool


__all__ = [
    "ConfigureReferenceFormatRequest",
    "ReferenceFormatResponse",
]
